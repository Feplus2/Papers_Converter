"""Stage 1: 元数据提取。

从 content_list 前几页文本中提取论文元数据（title, authors, date, abstract, doi 等）。
优先使用规则匹配，不足时调 DeepSeek 结构化提取。
"""

import json
import logging
import re

import config
from content_processor import _normalize_inline
from cover_detect import detect_cover_pages

logger = logging.getLogger(__name__)

# DOI 正则
_DOI_RE = re.compile(r"\b(10\.\d{4,}/[^\s,;\"'<>]+)")
# 年份正则
_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")



def extract_metadata(content_list: list[dict], use_llm: bool = True,
                     zotero_meta: dict | None = None) -> dict:
    """
    从 content_list 提取论文元数据。

    Args:
        content_list: MinerU 解析的 content_list.json 内容
        use_llm: 是否使用 LLM 辅助提取
        zotero_meta: Zotero/CSL-JSON 权威元数据（zotero_meta.get_zotero_meta 产出）。
            提供时 author/container-title/date/citekey 等以 Zotero 为准，LLM 只补 abstract

    Returns:
        frontmatter dict，字段对齐 paper-format-contract.md
    """
    # 检测并排除封面页（统一实现见 cover_detect；权威标题参与锚定否决）
    cover_pages = detect_cover_pages(
        content_list,
        title=(zotero_meta or {}).get("title", "") or "",
        doi=(zotero_meta or {}).get("doi", "") or "",
    )

    # 收集前几页的文本块（跳过噪声类型和封面页）
    noise_types = {"header", "footer", "page_number", "aside_text"}
    early_blocks = []
    for block in content_list:
        if block.get("page_idx", 0) > 4:
            break
        if block.get("page_idx", 0) in cover_pages:
            continue
        if block["type"] in noise_types:
            continue
        text = _normalize_inline(block.get("text", "").strip())
        if text:
            early_blocks.append({**block, "text": text})

    # 规则提取（基线/兑底）
    meta = _rule_extract(early_blocks, content_list)

    if zotero_meta:
        # Zotero/CSL-JSON 权威元数据覆盖（author/container-title/date/citekey 等以它为准）
        for key, val in zotero_meta.items():
            if val:
                meta[key] = val
        # LLM 只补 abstract（Zotero 与规则均未拿到摘要时）
        if not meta.get("abstract") and use_llm and config.DEEPSEEK_API_KEY:
            logger.info("  调用 LLM 补 abstract（其余字段以 Zotero 为准）...")
            llm_meta = _llm_extract(early_blocks)
            if llm_meta and llm_meta.get("abstract"):
                meta["abstract"] = llm_meta["abstract"]
    # LLM 主提取（启用时优先于规则，更鲁棒地处理各种非标准格式）
    elif use_llm and config.DEEPSEEK_API_KEY:
        logger.info("  调用 LLM 提取元数据（主提取）...")
        llm_meta = _llm_extract(early_blocks)
        if llm_meta:
            for key in ("title", "author", "abstract", "date", "doi",
                        "container-title", "keywords"):
                if llm_meta.get(key):
                    meta[key] = llm_meta[key]

    # CrossRef 兑底：仍有必填/主要字段缺失且有 DOI 时（契约建议的最后兑底）
    still_missing = [f for f in ("title", "abstract", "date", "container-title") if not meta.get(f)]
    if still_missing and meta.get("doi"):
        logger.info(f"  字段 {still_missing} 仍缺失，按 DOI 查 CrossRef 兑底...")
        cr = _crossref_fallback(meta["doi"])
        for key, val in cr.items():
            if val and not meta.get(key):
                meta[key] = val
    
    # 语言检测（含 CJK 字符判为中文）
    meta["lang"] = _detect_lang(meta, early_blocks)

    # 清洗标题（去掉首尾脚注/通讯标记如 \*\* * †）
    if meta.get("title"):
        meta["title"] = _clean_title(meta["title"])

    # 最终兑底
    if not meta.get("title"):
        meta["title"] = "Untitled"
    if not meta.get("author"):
        meta["author"] = [{"name": "Unknown"}]
    if not meta.get("date"):
        meta["date"] = ""
    if not meta.get("abstract"):
        meta["abstract"] = ""

    return meta


def _rule_extract(early_blocks: list[dict], full_list: list[dict]) -> dict:
    """基于规则的元数据提取"""
    meta = {}

    # --- Title: 第一个 text_level=1 的块 ---
    for block in early_blocks:
        if block.get("text_level") == 1 and block["type"] == "text":
            text = block["text"].strip()
            # 跳过太短或明显不是标题的
            if len(text) > 10 and not text.startswith("http"):
                meta["title"] = text
                break

    # --- DOI ---
    # DOI 在封面页/footer 中也可靠，扫描全部块
    for block in early_blocks:
        text = block.get("text", "")
        m = _DOI_RE.search(text)
        if m:
            doi = m.group(1).rstrip(".,;)")
            meta["doi"] = doi
            break
    # 从 footer 和封面页中找 DOI
    if "doi" not in meta:
        for block in full_list[:80]:
            m = _DOI_RE.search(block.get("text", ""))
            if m:
                meta["doi"] = m.group(1).rstrip(".,;)")
                break

    # --- Abstract ---
    # 摘要标记词：Abstract（通用）、Conspectus（ACS Accounts 综述）、摘 要（中文）
    for block in early_blocks:
        text = block.get("text", "")
        # 模式1: "Abstract:" / "Conspectus:" / "摘 要" 前缀（同一块内含正文）
        m = re.match(r"^(?:abstract|conspectus|摘\s*要)[:\.\s]+(.+)", text, re.IGNORECASE | re.DOTALL)
        if m:
            meta["abstract"] = m.group(1).strip()
            break
        # 模式2: 独立块标记（兼容 "A B S T R A C T" / "摘 要" 空格拆字样式）
        norm = re.sub(r"\s+", "", text).lower().rstrip(".:")
        if norm in ("abstract", "conspectus", "摘要"):
            # 下一个文本块是摘要正文
            idx = early_blocks.index(block)
            if idx + 1 < len(early_blocks):
                meta["abstract"] = early_blocks[idx + 1]["text"].strip()
            break

    # 模式3: 无显式 Abstract 标记的论文（Nature/Wiley 风格）
    # 摘要 = 标题与第一个章节标题之间的第一个长段落
    if not meta.get("abstract"):
        fb = _fallback_abstract(early_blocks)
        if fb:
            meta["abstract"] = fb

    # --- Keywords ---
    # 模式: "KEYWORDS a; b; c" / "Keywords: a, b, c" / "关键词 a; b; c"
    for block in early_blocks:
        text = block.get("text", "")
        m = re.match(r"^(?:key\s?words?|关键词)[:\.\s]+(.+)", text, re.IGNORECASE | re.DOTALL)
        if m:
            raw = m.group(1).strip()
            # MinerU 常把 KEYWORDS 行与后续正文合并成一个块；
            # 在首个引文标记 [数字] 处截断，避免吞入正文
            cit = re.search(r"\[\d", raw)
            if cit:
                raw = raw[:cit.start()]
            # 按分号、逗号或换行分割（不同期刊分隔符不同）
            parts = re.split(r"[;;,，、\n]", raw)
            keywords = [p.strip().rstrip(".。 ") for p in parts if p.strip()]
            # 过滤：过长的（误匹配的句子）或含句子结构的
            keywords = [k for k in keywords if 0 < len(k) <= 60 and ". " not in k]
            if keywords:
                meta["keywords"] = keywords[:10]
            break

    # --- Date/Year ---
    # 启发式 fallback 的年份合理性窗口：防止从 DOI（如 10.1149/2.0611916jes
    # 里的学会创立年 1916）或历史引文误提；显式日期行（received/accepted）不受限
    from datetime import date as _date_cls
    _max_year = _date_cls.today().year + 1

    def _plausible_years(text: str) -> list:
        return [y for y in re.findall(r"\b((?:19|20)\d{2})\b", text)
                if 1950 <= int(y) <= _max_year]

    # 先从 early_blocks 中找显式日期
    for block in early_blocks:
        text = block.get("text", "")
        m = re.search(r"(?:date|received|accepted|published)[:\s]*.*?((?:19|20)\d{2})", text, re.IGNORECASE)
        if m:
            meta["date"] = m.group(1)
            break
    # 如果没找到，扫描全部块（包括封面页/footer）中的年份信息
    if "date" not in meta:
        for block in full_list[:60]:
            text = block.get("text", "")
            # 从 citation 或封面信息中找
            if any(kw in text.lower() for kw in ("citation", "publication date", "published in", "©")):
                year_matches = _plausible_years(text)
                if year_matches:
                    meta["date"] = year_matches[-1]
                    break
    # 最后尝试：从 footer 中找版权年份
    if "date" not in meta:
        for block in full_list[:30]:
            if block["type"] == "footer":
                year_matches = _plausible_years(block.get("text", ""))
                if year_matches:
                    meta["date"] = year_matches[-1]
                    break

    # --- Authors ---
    # 查找标题后紧跟的作者行
    title_found = False
    for block in early_blocks:
        if block.get("text_level") == 1:
            title_found = True
            continue
        if title_found and block["type"] == "text" and not block.get("text_level"):
            text = block.get("text", "").strip()
            # 长度护栏：作者行通常较短，过长（>300）的块多半是摘要，跳过
            if len(text) > 300:
                continue
            # "By X and Y" / "By X, Y, and Z" 格式（Science/News 常见）
            if re.match(r"^By\s+", text, re.IGNORECASE):
                # 作者可能跨块（"By X and" + "Y"），合并以 and/, 结尾的连续短块
                combined = text
                idx = early_blocks.index(block)
                while combined.rstrip().endswith(("and", ",")) and idx + 1 < len(early_blocks):
                    nb = early_blocks[idx + 1]
                    if (nb.get("type") == "text" and not nb.get("text_level")
                            and len(nb.get("text", "")) < 200):
                        combined = combined.rstrip() + " " + nb.get("text", "").strip()
                        idx += 1
                    else:
                        break
                authors = _parse_author_line(combined)
                if authors:
                    meta["author"] = authors
                break
            # 含 $^{...}$ 上标（带编号的作者行）或逗号分隔的名字
            if ("$^{" in text or ", " in text) and _looks_like_authors(text):
                authors = _parse_author_line(text)
                if authors:
                    meta["author"] = authors
                break

    # --- Journal (container-title) ---
    for block in early_blocks:
        text = block.get("text", "")
        # 常见模式: "Published in\nScience" 或 footer 中的期刊名
        if text.lower().startswith("published in"):
            idx = early_blocks.index(block)
            if idx + 1 < len(early_blocks):
                meta["container-title"] = early_blocks[idx + 1]["text"].strip()
            break
    if "container-title" not in meta:
        for block in full_list[:30]:
            if block["type"] == "footer":
                text = block.get("text", "")
                # "Adv. Mater. 2005, 17, No. 7" 模式
                m = re.match(r"^([A-Z][a-z]*\.?\s*(?:[A-Z][a-z]*\.?\s*)+)", text)
                if m and len(m.group(1)) > 5:
                    meta["container-title"] = m.group(1).strip()
                    break

    # --- Zotero key (from folder name, passed externally) ---
    # 这个在 pipeline 中设置

    return meta


def _fallback_abstract(early_blocks: list[dict]) -> str | None:
    """无显式 Abstract 标记的论文（Nature/Wiley 风格）的摘要兑底提取。

    规律：摘要 = 标题之后、第一个章节标题之前的第一个长段落。
    跳过作者行（含 $^{ 上标且较短）和机构信息（以 $^ 开头）。
    """
    # 定位标题块
    title_idx = None
    for i, b in enumerate(early_blocks):
        if b.get("text_level") == 1 and b.get("type") == "text":
            title_idx = i
            break
    if title_idx is None:
        return None

    for b in early_blocks[title_idx + 1:]:
        # 碰到章节标题（text_level>=2）就停止
        if b.get("text_level") and b["text_level"] >= 2:
            break
        if b.get("type") != "text":
            continue
        text = b.get("text", "").strip()
        if len(text) < 150:
            continue
        # 跳过作者/机构块（多个 $^{ 上标标记，一人一个）
        if text.count("$^{") >= 3 or text.startswith("$^"):
            continue
        # 剥离出版信息前缀（Nature "...Check for updates <摘要>"）
        m = re.search(r"\bCheck for updates\s+", text)
        if m and m.start() < 250:
            text = text[m.end():].strip()
        text = re.sub(r"^Article\s+https?://doi\.org/\S+\s+", "", text)
        # 剥离后过短或仍是元数据开头，跳过
        if len(text) < 150:
            continue
        if re.match(r"^(received|accepted|published|doi|https?://|www\.|©|keywords?|to cite|cite as)\b", text, re.IGNORECASE):
            continue
        return text
    return None


def _crossref_fallback(doi: str) -> dict:
    """按 DOI 查 CrossRef 兑底提取元数据（契约建议的最后兑底）。

    CrossRef 开放 API，无需 key（提供 email 走 polite pool）。
    注意：CrossRef 的 abstract 常为 JATS XML，需剥离标签。
    """
    import urllib.request
    import urllib.parse
    url = f"https://api.crossref.org/works/{urllib.parse.quote(doi)}"
    req = urllib.request.Request(url, headers={
        "User-Agent": "PapersConverter/1.0 (mailto:zoterobrain@gmail.com)"
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        msg = data.get("message", {})
        result = {}
        # 标题
        titles = msg.get("title", [])
        if titles:
            result["title"] = titles[0]
        # 摘要（剥离 JATS XML 标签）
        abstract = msg.get("abstract", "")
        if abstract:
            abstract = re.sub(r"<[^>]+>", "", abstract)
            abstract = re.sub(r"\s+", " ", abstract).strip()
            result["abstract"] = abstract
        # 作者
        authors = []
        for a in msg.get("author", []):
            given = a.get("given", "")
            family = a.get("family", "")
            name = f"{given} {family}".strip()
            if name:
                authors.append({"name": name})
        if authors:
            result["author"] = authors
        # 日期（issued）
        issued = msg.get("issued", {}).get("date-parts", [[]])
        if issued and issued[0] and issued[0][0]:
            result["date"] = str(issued[0][0])
        # 期刊/会议名
        ct = msg.get("container-title", [])
        if ct:
            result["container-title"] = ct[0]
        logger.info(f"  CrossRef 命中: title={'title' in result} abstract={'abstract' in result}")
        return result
    except Exception as e:
        logger.warning(f"  CrossRef 查询失败: {e}")
        return {}


def _detect_lang(meta: dict, early_blocks: list[dict]) -> str:
    """检测文献语言：标题/摘要含 CJK 字符判为中文。"""
    text = (meta.get("title") or "") + (meta.get("abstract") or "")
    if len(text) < 20:
        text += " ".join(b.get("text", "") for b in early_blocks[:5])
    return "zh" if re.search(r"[\u4e00-\u9fff]", text) else "en"


def _clean_title(text: str) -> str:
    r"""清洗标题：去掉首尾的脚注/通讯标记（\*\* * † ‡ 及 $^{\dagger}$ 等上标形式）。"""
    t = text.strip()
    # 尾部 LaTeX 上标脚注 $^{\dagger}$ / $^{*}$ / $^{\ddagger}$（不误伤结尾正常化学式如 $TiO_2$）
    t = re.sub(r"\s*\$\^[^$]*\$\s*$", "", t)
    # 首尾裸标记 \*\* * † ‡ §
    t = re.sub(r"(?:\\?[*†‡§¶])+\s*$", "", t)
    t = re.sub(r"^\s*(?:\\?[*†‡§¶])+", "", t)
    return re.sub(r"\s{2,}", " ", t).strip()


def _looks_like_authors(text: str) -> bool:
    """判断文本是否像作者行"""
    # 含上标标记
    if "$^{" in text:
        return True
    # 含多个人名模式（首字母大写 + 姓氏）
    name_pattern = re.findall(r"[A-Z][a-z]+(?:\s+[A-Z]\.?)?", text)
    return len(name_pattern) >= 3


def _parse_author_line(text: str) -> list[dict]:
    """解析作者行，返回结构化作者列表"""
    # 去除 LaTeX 上标
    clean = re.sub(r"\$\^?\{[^}]*\}", "", text)
    # 去除 \dagger, * 等标记
    clean = re.sub(r"[\\$][\dagger*]", "", clean)
    # 去除通讯作者/贡献标记（☒ ☑ ✉ † 等 Unicode 符号）
    clean = re.sub(r"[☒☑✓✉†‡§¶#]+", "", clean)
    clean = clean.replace("$", "").strip()

    # 中文作者行：姓名用空格分隔（如 "师文睿 孙中强 宋忠锋"），每人 2–4 个汉字
    # 先去掉括号内的机构信息
    cjk_clean = re.sub(r"[（(][^）)]*[）)]", "", clean)
    if re.search(r"[\u4e00-\u9fff]", cjk_clean):
        tokens = [t for t in re.split(r"[\s,，、]+", cjk_clean) if t]
        cjk_names = [t for t in tokens if re.match(r"^[\u4e00-\u9fff]{2,4}$", t)]
        # 多数 token 是 2–4 字汉字姓名才认定为中文作者行
        if len(cjk_names) >= 2 and len(cjk_names) >= len(tokens) * 0.5:
            return [{"name": n} for n in cjk_names]

    # 处理分号分隔格式（"Zhao, Chenglong; Wang, Qidi; ..."）
    if ";" in clean and clean.count(";") >= 2:
        parts = [p.strip() for p in clean.split(";") if p.strip()]
        authors = []
        for part in parts:
            if "more author" in part.lower():
                continue
            name = re.sub(r"[\d\s]+", " ", part).strip()
            name = re.sub(r"\s{2,}", " ", name)
            if name and len(name) > 2:
                # "Zhao, Chenglong" → "Chenglong Zhao"
                if "," in name:
                    segments = [s.strip() for s in name.split(",", 1)]
                    if len(segments) == 2 and segments[1]:
                        name = f"{segments[1]} {segments[0]}"
                authors.append({"name": name})
        return authors

    # 逗号分隔格式（"Chenglong Zhao, Qidi Wang, ..."）
    # 先处理 "and" 连接
    clean = re.sub(r",?\s+and\s+", ", ", clean)
    # 去除 "By " 前缀
    clean = re.sub(r"^By\s+", "", clean, flags=re.IGNORECASE)
    parts = [p.strip() for p in clean.split(",") if p.strip()]

    authors = []
    for part in parts:
        # 跳过 "More Authors" 等
        if "more author" in part.lower():
            continue
        # 清理数字和多余空格
        name = re.sub(r"[\d\s]+", " ", part).strip()
        name = re.sub(r"\s{2,}", " ", name)
        if name and len(name) > 2:
            authors.append({"name": name})

    return authors


def _loads_lenient(content: str):
    r"""宽容解析 JSON：修复 LLM 输出中常见的非法转义（如 \~ \- ）。"""
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        # 把非标准转义的反斜杠转为双反斜杠
        sanitized = re.sub(r'\\(?!["\\/bfnrtu])', r'\\\\', content)
        return json.loads(sanitized)


def _llm_extract(early_blocks: list[dict]) -> dict:
    """调用 DeepSeek 提取元数据"""
    try:
        from openai import OpenAI
    except ImportError:
        logger.warning("openai 未安装，跳过 LLM 提取")
        return {}

    # 构建输入文本
    text_parts = []
    total_chars = 0
    for block in early_blocks:
        text = block.get("text", "")
        if total_chars + len(text) > config.METADATA_MAX_CHARS:
            break
        text_parts.append(text)
        total_chars += len(text)

    input_text = "\n\n".join(text_parts)

    prompt = """你是学术论文元数据提取专家。从论文开头文本中提取元数据，返回严格 JSON（不要 markdown 代码块、不要任何解释）。

需要提取的字段：
- title: 论文主标题。注意区分：不含副标题/dek（标题下方补充说明的一句话），不含栏目名（如 "LITHIUM BATTERIES"/"Review"/"INSIGHTS | PERSPECTIVES"/"Article"）
- authors: 作者数组，每项 {"name": "名 姓"}。作者可能以多种形式出现："By X and Y"、"X, Y, Z"、带上标编号 "X $^{1}$, Y $^{2}$"。请去掉 "By" 前缀、上标、机构名，只留人名
- date: 发表年份（4 位数字字符串）
- abstract: 摘要全文（完整，勿截断）。若有 "Abstract"/"摘要" 标记取其内容；若无明确标记，取标题与作者之后的第一个完整论述段落（Science/Nature 等常无 Abstract 标记）
- doi: DOI（如 "10.1126/science.abc5454"）
- container-title: 期刊/会议名（如 "Science"/"Nature"）
- keywords: 关键词数组（无则空数组）

无法确定的字段设为空字符串或空数组。

论文文本：
---
""" + input_text + "\n---"

    try:
        client = OpenAI(
            api_key=config.DEEPSEEK_API_KEY,
            base_url=config.DEEPSEEK_BASE_URL,
        )
        response = client.chat.completions.create(
            model=config.DEEPSEEK_MODEL,
            messages=[
                {"role": "system", "content": "你是学术论文元数据提取专家。只返回 JSON，不要 markdown 代码块。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.1,
            max_tokens=2000,
        )
        content = response.choices[0].message.content.strip()
        # 清理可能的 markdown 代码块
        if content.startswith("```"):
            content = re.sub(r"^```(?:json)?\s*", "", content)
            content = re.sub(r"\s*```$", "", content)

        data = _loads_lenient(content)

        # 转换为标准格式
        result = {}
        if data.get("title"):
            result["title"] = data["title"]
        if data.get("authors"):
            result["author"] = [
                {"name": a["name"]} if isinstance(a, dict) else {"name": str(a)}
                for a in data["authors"]
            ]
        if data.get("date"):
            result["date"] = str(data["date"])
        if data.get("abstract"):
            result["abstract"] = data["abstract"]
        if data.get("doi"):
            result["doi"] = data["doi"]
        if data.get("container-title"):
            result["container-title"] = data["container-title"]
        if data.get("keywords"):
            result["keywords"] = data["keywords"]

        return result

    except Exception as e:
        logger.warning(f"LLM 元数据提取失败: {e}")
        return {}
