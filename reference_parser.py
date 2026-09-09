"""P2.1 参考文献条目结构化（转换期）——产出 references.json。

施工依据：SageRead 仓库 docs/paper-link-rebuild-plan.md 的 P2.1 与关键澄清。
参考文献区的结构锚定在 content_processor 已完成（reference 块），本模块：

1. 规则预切分：reference 块 → 条目列表。行首编号（[N] / N. / N)）认条目起点，
   无编号块按悬挂缩进启发式并入上一条；块内多条目按编号标记切分，
   切分点须编号单调递增（防条目正文里的 [5] 被误当新条目）。
   编号解析复用 link_extractor._REF_NUM_RE——references.json 的 n 与
   paper.md 的 #ref-N 锚点同源，阅读器据此把卡片对上条目。
2. 辅助模型批量提取字段（禁思考，JSON 数组）：{n, raw, title, authors[],
   year, venue, doi?}；条目数与规则切分差 >20% 或任何失败 → 整段降级为
   规则切分（title=raw，year 用正则尽力而为），不阻塞转换。
3. DOI 确定性正则层（10\\.\\d{4,9}/\\S+ 从 raw 提取）与 LLM 结果互校，
   冲突以正则为准；LLM 给出的 doi 也要过同一正则形态校验（防幻觉）。
4. references.json 落在论文目录（与 paper.md 同级），纯增量产物，
   不触碰 paper.md 一个字节。无参考文献区 → 不产文件、不报错。
"""

import json
import logging
import re
from pathlib import Path

import config
import llm_thinking
from link_extractor import _REF_NUM_RE

logger = logging.getLogger(__name__)

# 条目起点（复用 _REF_NUM_RE 语义："[12] " / "12. " / "12) " / "12 X. Y.,"）
_ENTRY_START_RE = _REF_NUM_RE
# 块内多条目的切分候选："[N] "（无序容忍双栏交错）；". N. " 与 ". N "（裸编号）
# 须严格连号（挡页码/卷号伪起点，madler '390. VDI Verlag' 实测）
_MARK_BRACKET_RE = re.compile(r"\[(\d{1,4})\]\s+(?=[A-Z\"'(])")
_MARK_DOT_RE = re.compile(r"(?<=\.\s)(\d{1,4})\.\s+(?=[A-Z])")
_MARK_BARE_RE = re.compile(r"(?<=\.\s)(\d{1,4})\s+(?=[A-Z])")
# 块首条目起点同样要求编号后接大写形态（防续行块以 [5] 引文开头被当成新条目；
# 裸编号形态 RSC 式 "1 J. Y. Hwang, ..."，wang2024 实测）
_ENTRY_START_STRICT_RE = re.compile(
    r"^\s*(?:\[(\d{1,4})\]\s+(?=[A-Z\"'(])|(\d{1,4})[.\)]\s+(?=[A-Z\"'(])"
    r"|(\d{1,4})\s+(?=[A-Z]))")
# DOI 确定性正则层（字符集明确，零幻觉）；尾部标点不在 DOI 内
_DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"'<>\[\]]+")
_DOI_FULL_RE = re.compile(r"^10\.\d{4,9}/\S+$")
_DOI_TAIL = ".,;:"
# arXiv 编号确定性正则层（新式 2011.12414 / 旧式 hep-th/0204074，
# 可带子类 astro-ph.CO；版本号 vN 不收——canonical 匹配用裸号）
_ARXIV_NEW_RE = re.compile(r"arXiv:\s*(\d{4}\.\d{4,5})(?:v\d+)?", re.I)
_ARXIV_OLD_RE = re.compile(r"arXiv:\s*([a-z\-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?", re.I)
# 规则降级模式的年份提取（1990-2099 的独立四位数）
_YEAR_RE = re.compile(r"(?<![\d./])((?:19|20)\d{2})(?!\d)")

_LLM_CHUNK = 30        # 每次调用的条目数
_LLM_RAW_TRUNC = 600   # 送 LLM 的单条 raw 截断（超长条目罕见）


def extract_doi(raw: str) -> str | None:
    """从条目原文确定性提取 DOI（尾部标点剥离；右括号按配对深度剥离，
    保留 DOI 自带的平衡括号如 10.1016/0550-3213(85)90022-7）。"""
    m = _DOI_RE.search(raw or "")
    if not m:
        return None
    doi = m.group(0).rstrip(_DOI_TAIL)
    while doi.endswith(")") and doi.count(")") > doi.count("("):
        doi = doi[:-1]
    return doi or None


def extract_arxiv(raw: str) -> str | None:
    """从条目原文确定性提取 arXiv 编号（新式 2011.12414 优先，
    旧式 hep-th/0204074 兜底；版本号 vN 不收）。"""
    m = _ARXIV_NEW_RE.search(raw or "")
    if m:
        return m.group(1)
    m = _ARXIV_OLD_RE.search(raw or "")
    return m.group(1) if m else None


def _clean_title(title, raw: str) -> str | None:
    """title 卫生：剥 [N]/N. 枚举前缀；arXiv 括号段及其后一切截掉（标识符
    不是标题的一部分）；剥完为空、或与 raw 复读一致 → None（宁缺毋滥，
    绝不把整条 raw 塞进 title）。"""
    t = re.sub(r"\s+", " ", str(title or "")).strip()
    if not t:
        return None
    t = re.sub(r"^\s*(?:\[\d{1,4}\]|\d{1,4}[.\)])\s+", "", t)  # 枚举前缀
    t = re.split(r"\[\s*arXiv\s*:", t, flags=re.I)[0]         # [arXiv:...] 截断
    t = t.strip().strip('"').strip()
    t = t.rstrip(".,;:[]() \t\"'")
    if len(t) < 4:
        return None
    # raw 复读判定：raw 过同款前缀/尾部清洗后仍一致 → 不是真标题
    r = re.sub(r"\s+", " ", raw).strip()
    r = re.sub(r"^\s*(?:\[\d{1,4}\]|\d{1,4}[.\)])\s+", "", r)
    r = re.split(r"\[\s*arXiv\s*:", r, flags=re.I)[0].strip().rstrip(".,;:[]() \t\"'")
    if t == r:
        return None
    return t


def split_reference_entries(blocks: list) -> list[dict]:
    """reference 块 → 条目列表 [{"n": int|None, "raw": str}]。

    切分规则：块首/块内的编号标记认条目起点。防误切靠"编号后接大写形态"
    （条目首部是作者姓氏，"citing [5] here" 这类句中引文不匹配）——
    不做编号单调要求：双栏文献列表被引擎交错输出时（[3] 片段先于 [2] 出现）
    条目起点本就乱序，单调闸门会误并真条目（宇宙弦实测）。
    无编号块并入上一条（悬挂缩进续行）；无历史条目时记 n=None。
    """
    entries: list[dict] = []
    last_dot = 0  # 点号式（"N. "）与裸编号（"N X. Y.,"）条目须严格连号（跨块延续）；
    # 括号式（"[N] "）不做连号要求——双栏文献列表被引擎交错输出时
    # 括号条目起点本就乱序（宇宙弦实测），由大写形态闸门防句中引文误切
    for b in blocks:
        if b.kind != "reference":
            continue
        text = (b.content or "").strip()
        if not text:
            continue
        # 收集切分点：块首（严格形态）+ 块内编号标记
        marks = []  # (位置, n, 是否连号式)
        m0 = _ENTRY_START_STRICT_RE.match(text)
        if m0:
            if m0.group(1) is not None:
                marks.append((0, int(m0.group(1)), False))
            elif m0.group(2) is not None and int(m0.group(2)) == last_dot + 1:
                marks.append((0, int(m0.group(2)), True))
            elif m0.group(3) is not None and int(m0.group(3)) == last_dot + 1:
                marks.append((0, int(m0.group(3)), True))
        for m in _MARK_BRACKET_RE.finditer(text):
            if m.start() > 0:
                marks.append((m.start(), int(m.group(1)), False))
        for m in _MARK_DOT_RE.finditer(text):
            pos = m.start(1)
            if int(m.group(1)) == last_dot + 1 \
                    and all(pos != mk[0] for mk in marks):
                marks.append((pos, int(m.group(1)), True))
        for m in _MARK_BARE_RE.finditer(text):
            pos = m.start(1)
            if int(m.group(1)) == last_dot + 1 \
                    and all(pos != mk[0] for mk in marks):
                marks.append((pos, int(m.group(1)), True))
        marks.sort()
        if not marks:
            if entries:
                entries[-1]["raw"] += " " + text  # 悬挂缩进续行
            else:
                entries.append({"n": None, "raw": text})
            continue
        for k, (pos, n, is_dot) in enumerate(marks):
            end = marks[k + 1][0] if k + 1 < len(marks) else len(text)
            raw = text[pos:end].strip()
            if len(raw) < 6:
                continue  # 碎片（如孤立 "]"）不收
            entries.append({"n": n, "raw": raw})
            if is_dot:
                last_dot = n
    # 精确去重（末页重复文献列表等版面伪影：同 n 同文只留第一份）
    seen = set()
    deduped = []
    for e in entries:
        key = (e["n"], e["raw"][:40])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(e)
    # 作者-年份制（APA 等）整条目无编号：一个编号起点都没找到时，
    # 每个 reference 块即一条（madler2001 实测），n=None 由阅读器按位置对齐。
    # 续行判定：APA 条目首部必有 (年份)，块首 100 字符内无 (19xx/20xx) 的
    # 视为上一条的续行（如 "Wiley." 出版社残段）
    if deduped and not any(e["n"] is not None for e in deduped):
        deduped = []
        for b in blocks:
            if b.kind != "reference":
                continue
            text = (b.content or "").strip()
            if len(text) < 6:
                continue
            if deduped and not re.search(r"^.{0,100}\((?:19|20)\d{2}[a-z]?\)", text):
                deduped[-1]["raw"] += " " + text
            else:
                deduped.append({"n": None, "raw": text})
    return deduped


# ============================================================
# LLM 批量字段提取（禁思考；任何失败 → None，调用方整段降级）
# ============================================================

_PROMPT = """你是文献条目解析器。把下列论文参考文献条目逐条解析，返回严格 JSON 数组
（不要 markdown 代码块、不要解释），每条：
{"n": 条目编号(整数,照抄输入), "title": "文献标题", "authors": ["作者1", "作者2"],
"year": 发表年份(整数), "venue": "期刊/会议名", "doi": "10.xxxx/... 或 null"}
规则：title/authors/year/venue 从条目原文提取，不确定给 null/空数组，禁止编造；
authors 保留原文缩写形态即可；doi 只给条目里明确出现的。

条目：
%s"""


def _llm_extract(entries: list[dict], use_llm: bool) -> list[dict] | None:
    """辅助模型批量提取字段。失败/异常/非 JSON → None（整段降级规则切分）。"""
    if not use_llm or not config.DEEPSEEK_API_KEY:
        return None
    try:
        from openai import OpenAI
    except ImportError:
        return None
    client = OpenAI(api_key=config.DEEPSEEK_API_KEY,
                    base_url=config.DEEPSEEK_BASE_URL)
    items: list[dict] = []
    try:
        for i in range(0, len(entries), _LLM_CHUNK):
            chunk = entries[i:i + _LLM_CHUNK]
            listing = json.dumps(
                [{"n": e["n"], "raw": e["raw"][:_LLM_RAW_TRUNC]} for e in chunk],
                ensure_ascii=False)
            # 空响应/非 JSON 响应重试一次（martins2000 实测 LLM 偶发空回
            # "Expecting value: line 1 column 1"），再失败才干净降级
            data = None
            for attempt in range(2):
                resp = llm_thinking.chat_create(
                    client,
                    model=config.DEEPSEEK_MODEL,
                    messages=[
                        {"role": "system",
                         "content": "你是文献条目解析器。只返回 JSON 数组。"},
                        {"role": "user", "content": _PROMPT % listing},
                    ],
                    temperature=0.0,
                    max_tokens=4096,
                )
                content = (resp.choices[0].message.content or "").strip()
                content = re.sub(r"^```(?:json)?\s*", "", content)
                content = re.sub(r"\s*```$", "", content)
                try:
                    data = json.loads(content)
                except json.JSONDecodeError:
                    try:
                        data = json.loads(
                            re.sub(r'\\(?!["\\/bfnrtu])', r"\\\\", content))
                    except json.JSONDecodeError:
                        data = None
                if isinstance(data, list):
                    break
                if attempt == 0:
                    logger.warning("  参考文献 LLM 返回空/非 JSON，重试一次")
            if not isinstance(data, list):
                return None
            items.extend(x for x in data if isinstance(x, dict))
    except Exception as e:
        logger.warning(f"  参考文献 LLM 提取失败（降级规则切分）: {e}")
        return None
    return items


def _san_year(v) -> int | None:
    try:
        y = int(str(v).strip())
        return y if 1900 <= y <= 2099 else None
    except (TypeError, ValueError):
        return None


def _san_authors(v) -> list:
    if not isinstance(v, list):
        return []
    return [str(a).strip() for a in v if str(a).strip()][:30]


def build_references(entries: list[dict], use_llm: bool = True) -> tuple[list[dict], str]:
    """规则切分 + LLM 字段 + DOI 正则互校。返回 (条目列表, source)。

    source: "llm"（LLM 提取成功）/ "rule"（降级：title=raw，其余尽力而为）。
    """
    items = _llm_extract(entries, use_llm)
    source = "llm"
    if items is None or abs(len(items) - len(entries)) > max(2, 0.2 * len(entries)):
        if items is not None:
            logger.warning(
                f"  参考文献 LLM 条目数 {len(items)} 与规则切分 {len(entries)} 差 >20%"
                "（整段降级规则切分）")
        items = None
        source = "rule"
    by_n: dict[int, dict] = {}
    unnum_items: list[dict] = []  # n 不可解析的 LLM 条目（与无编号规则条目按序配对）
    if items:
        for it in items:
            try:
                n = int(str(it.get("n")).strip())
            except (TypeError, ValueError):
                unnum_items.append(it)
                continue
            by_n.setdefault(n, it)
    # 全无编号列表（APA 作者-年份制）：LLM 常自作主张补 1..k 编号而非照抄 null，
    # 此时按位置配对（madler2001 实测）
    all_unnum = bool(entries) and all(e["n"] is None for e in entries)

    out = []
    unnum_pos = 0
    for i, e in enumerate(entries):
        raw = e["raw"]
        it = None
        if items:
            if all_unnum:
                it = items[i] if i < len(items) else None
            elif e["n"] is not None:
                it = by_n.get(e["n"])
            elif unnum_pos < len(unnum_items):
                it = unnum_items[unnum_pos]
                unnum_pos += 1
        if it is not None:
            # title 卫生：剥枚举前缀、截 arXiv 括号段、禁 raw 复读（抽不出 → None）
            title = _clean_title(it.get("title"), raw)
            authors = _san_authors(it.get("authors"))
            year = _san_year(it.get("year"))
            venue = str(it.get("venue") or "").strip() or None
            doi = it.get("doi")
            doi = str(doi).strip() if doi else None
            # LLM 的 doi 也要过正则形态校验（防幻觉），不过形态即弃
            if doi and not _DOI_FULL_RE.match(doi):
                doi = None
        else:
            # 降级规则切分：title 抽不出就置 None，绝不把整条 raw 塞进去
            title, authors, venue, doi = None, [], None, None
            m = _YEAR_RE.search(raw)
            year = int(m.group(1)) if m else None
        # DOI 确定性正则层：与 LLM 互校，冲突以正则为准（含 LLM 缺失时补齐）
        doi_re = extract_doi(raw)
        if doi_re:
            doi = doi_re
        # arXiv 编号确定性正则层（LLM 不参与此字段，正则零幻觉直取）
        arxiv_id = extract_arxiv(raw)
        out.append({"n": e["n"], "raw": raw, "title": title, "authors": authors,
                    "year": year, "venue": venue, "doi": doi,
                    "arxiv_id": arxiv_id})
    return out, source


def prepare_references(blocks: list, use_llm: bool = True) -> dict | None:
    """切分+提取参考文献条目，返回 references.json 的 payload；无文献区 → None。

    须在链接注入之前调用（raw 取未注入链接语法的净文本）。
    """
    entries = split_reference_entries(blocks)
    if not entries:
        return None
    refs, source = build_references(entries, use_llm=use_llm)
    return {"version": 1, "source": source, "count": len(refs),
            "references": refs}


def dump_references(payload: dict, paper_dir: Path) -> Path:
    """references.json 落盘（与 paper.md 同级，纯增量产物）。"""
    path = Path(paper_dir) / "references.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n",
                    encoding="utf-8", newline="\n")
    return path
