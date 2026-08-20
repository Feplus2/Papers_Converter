"""P1：保留 PDF 原生链接（link annotations）——提取、目标映射与注入。

施工依据：SageRead 仓库 docs/paper-link-rebuild-plan.md 的 P1 部分。
OCR 引擎（MinerU/PaddleOCR）只产文本流，PDF 的 link annotations 全部丢失；
本模块用 PyMuPDF 把它们捡回来注入 paper.md 的块内容（ProcessedBlock.content）。

三层结构：
1. 提取：page.get_links() 拿 LINK_GOTO / LINK_NAMED / LINK_URI；
   源区域文字用 rawdict 字符 bbox 精确截取（字符中心点落入链接矩形），
   禁止用块文本模糊匹配当链接源文字；LINK_NAMED 优先用链接自带 page/to，
   否则经 doc.resolve_names() 解析，解析失败即放弃该条（不猜）。
2. 目标映射：(page, y) → 该页覆盖目标行的最终块：
   图/表块 → #fig-N / #tab-N（N 取图注/表注编号）；参考文献条目 → #ref-N
   （N 从条目首部 [N]/N. 解析）；其余 → 最近 heading 的 #sec-*。
   每条链接都做编号一致性校验（源文字编号 == 目标块编号），位置映射失配时
   回退到"同号唯一块"（named dest 的名字本身即目标声明，非编造）；
   编号对不上且无唯一同号块 → 放弃该链接保留纯文本（宁缺毋滥）。
3. 注入：rawdict 字符区间经 NFKC/小写/空白折叠骨架上的 difflib 对齐，
   映射为块文本的字符级偏移（同一块多个 [12] 各自对准各自的链接注释）；
   区间守卫：块内子串骨架须与链接源文字一致；数学段（$...$/$$...$$）内不注入。

输出契约（与 SageRead 阅读器侧对接，一字不可改）：
  锚点：参考文献条目首 <a id="ref-N"></a>；图/表块 <a id="fig-N"></a> /
  <a id="tab-N"></a>；节目标 <a id="sec-..."></a>（heading 文本 slug 化）；
  链接：[[12]](#ref-12)、[Fig. 3](#fig-3)、[text](https://...)。
锚点 id 由 block_anchor_id() 统一计算，renderer 与注入器共用，保证一致。
"""

import difflib
import logging
import re
import unicodedata
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# URI 链接白名单：可见文字本身是 URL/DOI/邮箱（页眉页脚杂链不注入）；
# 兜底：落在参考文献条目块内的 URI 链接（期刊名/卷期页样式的 DOI 超链）直接放行
_URI_TEXT_RE = re.compile(
    r"(?:https?://|www\.|doi\.org/|10\.\d{4,9}/\S|arxiv:"
    r"|[\w-]+(?:\.[\w-]+)+/\S{3,})", re.I)  # 无 scheme 的 domain/path 形式
# 引文源文字：[12] / [12,13] / [12-14]；裸数字仅在 named dest 声明 cite.* 时可信
_CITE_TEXT_RE = re.compile(r"^\[?\s*(\d{1,4})\s*(?:[,\u2013\u2014;-]\s*\d{1,4}\s*)*\]?$")
_NUM_TEXT_RE = re.compile(r"(\d+(?:\.\d+)*)")
_FIG_TEXT_RE = re.compile(r"^(?:Figs?\.?|Figures?)\s*(\d+(?:\.\d+)*)[a-z]?\s*[.,\)]?$", re.I)
_TAB_TEXT_RE = re.compile(r"^Tables?\s*(\d+(?:\.\d+)*)\s*[.,\)]?$", re.I)
_SEC_TEXT_RE = re.compile(
    r"^(?:(?:Sections?|Sec\.?|§)\s*)?([IVXLC]+|\d+(?:\.\d+)*)\s*[.,\)]?$", re.I)

# 块内容里的编号解析（锚点 id 用）
_REF_NUM_RE = re.compile(r"^\s*\[(\d{1,4})\]|^\s*(\d{1,4})[.\)]\s")
_FIG_NUM_RE = re.compile(r"^\s*Figure\s+(\d+(?:\.\d+)*)", re.I)
_TAB_NUM_RE = re.compile(r"^\s*Table\s+(\d+(?:\.\d+)*)", re.I)
_HEAD_NUM_RE = re.compile(r"^\s*([IVXLC]+|\d+(?:\.\d+)*)[.\):]?\s")
# 公式 \tag 编号（与 content_processor._EQUATION_TAG_RE 同形态，捕获编号内容）
_EQ_TAG_RE = re.compile(r"\\tag\*?\s*\{([^{}]*)\}")
# 公式引用源文字：(5) / (A2) / Eq. (5) / Eqs. (12)——hyperref 的 equation.N
# dest 名是内部计数器而非印刷编号（实测 equation.4 的可见文字是 (5)），
# 编号必须取自源文字，dest 名只用来定性类型
_EQ_NUM_TEXT_RE = re.compile(
    r"^(?:(?:Eqs?\.?|Equations?)\s*)?\(\s*([A-Za-z]?\d[\w]*)\s*\)$", re.I)
_EQ_NUM_BARE_RE = re.compile(r"^\(\s*([A-Za-z]?\d[\w]*)\s*\)$")
# named equation.* 的源文字放宽到裸数字（hyperref 链接矩形只覆盖 "(5)" 里的数字，
# 括号在矩形外）；dest 已声明类型，语义充分
_EQ_NUM_DEST_RE = re.compile(r"^\(?\s*([A-Za-z]?\d[\w]*)\s*\)?$")

# 数学段（行内 $...$ 与展示 $$...$$）：内部一律不注入
_MATH_SPAN_RE = re.compile(r"\$\$.+?\$\$|\$[^$\n]+?\$", re.S)

# 链接源文字长度上限（跨行 URL 也不过几十字符；超长多为脏矩形，放弃）
_MAX_LINK_TEXT = 100

# URI 链接的显示文字裁掉首尾括号类字符（链接矩形常把包围 URL 的括号一并覆盖）。
# 注意不裁方括号："[astro-ph.CO]" 这类分类标记的括号是正文一部分，
# 裁掉会破坏相邻区间合并后的括号平衡
_URI_TRIM_HEAD = "({<'\"\u2018\u201c"
_URI_TRIM_TAIL = ")}.,;:'\">\u2019\u201d"

# 骨架化的显式字符折叠（NFKC 不覆盖的弯引号/连接号）
_CHAR_FOLD = str.maketrans({
    "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
    "\u2013": "-", "\u2014": "-", "\u2212": "-",
})


def _slug(text: str) -> str:
    """heading 文本 slug 化（sec-* 锚点用）：NFKC + 小写，非字母数字折叠为 -"""
    t = unicodedata.normalize("NFKC", text).casefold()
    t = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "-", t).strip("-")
    return t[:80].strip("-")


def block_anchor_id(block) -> str | None:
    """计算块对应的锚点 id（renderer 发射与注入器目标映射共用，保证一字不差）。

    heading → sec-<slug>；reference → ref-N（首部 [N]/N. 解析；解析不到 → None，
    无编号条目无法校验编号一致性，按"宁缺毋滥"不生成锚点）；
    image/table_image/table → fig-N / tab-N（N 取图注/表注编号，无编号 → None）。
    """
    if block.kind == "heading":
        s = _slug(block.content or "")
        return f"sec-{s}" if s else None
    if block.kind == "reference":
        m = _REF_NUM_RE.match(block.content or "")
        if m:
            return f"ref-{m.group(1) or m.group(2)}"
        return None
    if block.kind in ("image", "table_image"):
        m = _FIG_NUM_RE.match((block.content or "").strip())
        if m:
            return f"fig-{m.group(1)}"
        m = _TAB_NUM_RE.match(((block.content or "") + " " + (block.caption or "")).strip())
        if m:
            return f"tab-{m.group(1)}"
        return None
    if block.kind == "table":
        m = _TAB_NUM_RE.match((block.caption or "").strip())
        if m:
            return f"tab-{m.group(1)}"
    if block.kind == "equation":
        # 公式编号从内容 \tag{N} 解析（N 可为 50 或 A2 附录形态）；同块多 \tag
        # 按 _dedup_equation_tags 既有规则取准（内容最长者，并列取最末）；
        # 无 \tag 的公式不生成锚点
        tags = _EQ_TAG_RE.findall(block.content or "")
        if tags:
            keep = max(range(len(tags)), key=lambda i: (len(tags[i]), i))
            return f"eq-{tags[keep]}"
        return None
    return None


# ============================================================
# 骨架化：NFKC + casefold + 空白折叠，保留 骨架下标 ↔ 原文下标 双向映射。
# 大小写/合字/弯引号差异被抹平，使引擎文本与 PDF rawdict 字符流可对齐。
# ============================================================

def _skeletonize(text: str) -> tuple[str, list[int]]:
    """返回 (骨架串, 骨架下标→原文下标 映射)。"""
    sk: list[str] = []
    s2o: list[int] = []
    prev_ws = True  # 前导空白直接丢弃；词间空白折叠为单个空格
    pending = False
    pend_from = 0  # 待补空格的原文下标（空白字符自身，非其后首个非空白字符——
    # 误记为后者会让 r2s 把该字符映射到空格拉槽，区间起点偏一，cosmic 实测）
    for i, ch in enumerate(text):
        # NFKC 不抹平弯引号/连接号，显式归一（引擎与 PDF 字符流常因此错位）
        ch = ch.translate(_CHAR_FOLD)
        for c in unicodedata.normalize("NFKC", ch).casefold():
            if c.isspace():
                if not pending:
                    pend_from = i
                pending = True
                continue
            if pending and not prev_ws:
                sk.append(" ")
                s2o.append(pend_from)
            sk.append(c)
            s2o.append(i)
            prev_ws = False
            pending = False
    return "".join(sk), s2o


class _Page:
    """单页 rawdict 字符流：raw 原文、字符 bbox、骨架、双向映射、行区间。"""

    __slots__ = ("raw", "char_rects", "raw2char", "skel", "s2r", "r2s", "lines")

    def __init__(self, fitz_page):
        import fitz

        raw_parts: list[str] = []
        self.char_rects: list = []          # 字符序（不含插入的换行）
        char_raw_pos: list[int] = []        # 字符序 → raw 下标
        raw_lines: list[tuple[int, int, object]] = []  # raw 下标区间 + 行矩形
        pos = 0
        for b in fitz_page.get_text("rawdict")["blocks"]:
            for ln in b.get("lines", []):
                chars = [c for s in ln["spans"] for c in s["chars"]]
                if not chars:
                    continue
                line_start = pos
                for c in chars:
                    raw_parts.append(c["c"])
                    self.char_rects.append(fitz.Rect(c["bbox"]))
                    char_raw_pos.append(pos)
                    pos += 1
                raw_lines.append((line_start, pos, fitz.Rect(ln["bbox"])))
                raw_parts.append("\n")
                pos += 1
            raw_parts.append("\n")
            pos += 1
        self.raw = "".join(raw_parts)
        # raw 下标 → 字符序（插入的 "\n" → -1）
        self.raw2char = [-1] * len(self.raw)
        for ci, rp in enumerate(char_raw_pos):
            self.raw2char[rp] = ci
        self.skel, self.s2r = _skeletonize(self.raw)
        # raw 下标 → 骨架下标（被骨架化折叠/丢弃的字符 → None）
        self.r2s: list[int | None] = [None] * len(self.raw)
        for si, ri in enumerate(self.s2r):
            if self.r2s[ri] is None:
                self.r2s[ri] = si
        # 行区间换算到骨架坐标（目标定位用）
        self.lines: list[tuple[int, int, object]] = []
        for r0, r1, rect in raw_lines:
            s0 = _raw_to_skel(self, r0, forward=True)
            s1 = _raw_to_skel(self, r1 - 1, forward=False)
            if s0 is None or s1 is None or s1 <= s0:
                continue
            self.lines.append((s0, s1 + 1, rect))

def _rect_contains(rect, xy) -> bool:
    x, y = xy
    return rect.x0 <= x <= rect.x1 and rect.y0 <= y <= rect.y1


def _raw_to_skel(page: _Page, raw_idx: int, forward: bool) -> int | None:
    """raw 下标 → 骨架下标；被折叠字符就近取（forward=True 向后找）。"""
    n = len(page.raw)
    if forward:
        for i in range(raw_idx, n):
            if page.r2s[i] is not None:
                return page.r2s[i]
    else:
        for i in range(min(raw_idx, n - 1), -1, -1):
            if page.r2s[i] is not None:
                return page.r2s[i]
    return None


# 出版商书签式 named dest：尾部词干+编号（Elsevier bib0001/FIG23/TBL1/eqn0001、
# RSC bm_CR1/bm_Fig1/bm_Equ1 等；aff/fn/cor/MOESM 等非正文目标不匹配此式，仍放弃）
_PUB_DEST_RE = re.compile(
    r"(bib|bibr|refs?|fig|tbl|tab|eqn|equ?|cr)s?0*(\d+)$", re.I)
_PUB_DEST_CAT = {
    "bib": "ref", "bibr": "ref", "ref": "ref", "refs": "ref", "cr": "ref",
    "fig": "fig", "tbl": "tab", "tab": "tab",
    "eqn": "eq", "eq": "eq", "equ": "eq",
}


# 脚注类 named dest：hyperref footnote.N/Hfootnote.N、equation.N-footnote.M、
# frontmatter.N、出版社 fn1（Elsevier MAC…FN1 等）。aff/cor 机构/通讯作者
# 标记语义太宽（字母标记难以无歧义校验），不匹配此式、维持放弃
_FN_DEST_RE = re.compile(r"(?:h?footnote|frontmatter|fn)\.?\d+$", re.I)
# 符号脚注标记归一（∗/*/ast、dagger 等各写归一到单字符键）
_FN_SYM_MAP = {
    "*": "*", "∗": "*", "†": "†", "‡": "‡", "§": "§", "¶": "¶",
}


def _footnote_symbol_of(block) -> str | None:
    """footnote 块内容的首标记符号（归一键）；无符号标记 → None。"""
    t = (block.content or "").lstrip()
    m = re.match(r"^(?:\\?([\*†‡§¶])|\$\^\{?\\?(?:dagger|ddagger|ast|star)\b)",
                 t)
    if not m:
        return None
    if m.group(1):
        return _FN_SYM_MAP.get(m.group(1))
    tok = m.group(0)
    if "dagger" in tok:
        return "‡" if "ddagger" in tok else "†"
    return "*"  # ast/star


def _brackets_balanced(text: str) -> bool:
    """方括号是否平衡（Markdown 链接显示文字的要求；深度不为负且归零）。"""
    depth = 0
    for ch in text:
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


# 完整引文簇形态：[2, 3] / [4–8] / [15, 19–22]（分隔符逗号/连字符/短横线）
_CLUSTER_FULL_RE = re.compile(
    r"^\[\d+(?:\s*[,\u2013\u2014-]\s*\d+)*\]$")
# 簇部分矩形的源文字形态：数字 + 至多一个括号/逗号/短横线前后缀（"[2," / "3]" /
# "[4–" / "–8]" / "19–"），纯数字（上标引文伪影）也可作候选——拼接校验会拦住
_CLUSTER_PART_RE = re.compile(
    r"^[\[\],\u2013\u2014-]?\s*\d{1,4}\s*[\[\],\u2013\u2014-]?$")
# 簇合并的 x 向间隙上限（实测 hyperref 拆分间隙 3–7pt；下限允许约一字符宽
# 的矩形重叠——"[4–"+"–8]" 的短横线字符常被相邻两矩形共享）与行心 y 差上限
_CLUSTER_X_GAP_PT = 10.0
_CLUSTER_X_OVERLAP_PT = 6.0
_CLUSTER_Y_TOL_PT = 2.5


# span_for_rect：链接矩形 → raw 字符区间 [c0, c1)（字符中心点落入矩形）
def _span_for_rect(page: _Page, rect) -> tuple[int, int] | None:
    hit = [i for i, r in enumerate(page.char_rects)
           if _rect_contains(rect, ((r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2))]
    if not hit:
        return None
    first, last = hit[0], hit[-1]
    c0 = c1 = None
    for ri, ci in enumerate(page.raw2char):
        if ci == first and c0 is None:
            c0 = ri
        if ci == last:
            c1 = ri + 1
    if c0 is None or c1 is None or c1 <= c0:
        return None
    return c0, c1


@dataclass
class _Link:
    page: int               # 源页（0 基）
    c0: int                 # 源矩形覆盖的 raw 字符区间 [c0, c1)
    c1: int
    text: str               # rawdict 字符级源文字
    uri: str = ""
    dest_page: int = -1     # 内部目标页（GOTO/NAMED 解析后）
    dest_x: float = 0.0
    dest_y: float = 0.0
    dest_name: str = ""     # nameddest（cite.* / figure.N / section* / ...）
    cluster: int = -1       # 引文簇组号（≥0 时为簇部分矩形，须整组同块注入）
    fn_target: int = -1     # 脚注链接的目标 footnote 块下标（label 延迟落块用）


@dataclass
class LinkResult:
    anchors: set = field(default_factory=set)  # 被链接指向的锚点 id（renderer 据此发射）
    injected: int = 0
    dropped: int = 0
    stats: dict = field(default_factory=dict)  # 类别 -> [注入数, 放弃数]


def _link_rect(page: _Page, lk: _Link):
    """链接源区间的字符 bbox 并集（x0, y0, x1, y1；仅用于簇邻近判定）。"""
    rects = [page.char_rects[page.raw2char[i]]
             for i in range(lk.c0, lk.c1)
             if 0 <= i < len(page.raw2char) and page.raw2char[i] >= 0]
    if not rects:
        return None
    return (min(r.x0 for r in rects), min(r.y0 for r in rects),
            max(r.x1 for r in rects), max(r.y1 for r in rects))


def _merge_citation_clusters(links: list, pages: list) -> list:
    """引文簇/区间部分矩形合并（hyperref 把 [2, 3] 拆成 "[2,"→ref2 + "3]"→ref3、
    [4–8] 拆成 "[4–"→ref4 + "–8]"→ref8；部分 token 过不了单条源文字校验）。

    判定（保守，任一步不符维持原样）：同页、行心 y 差 ≤2.5pt、x 间隙 ≤10pt 的
    cite.* 部分矩形连成 run（≥2 条），run 覆盖的完整原文恰构成合法引文形态
    [\\d+(,\\s*\\d+)*] 或 [\\d+–\\d+]（含混合 [15, 19–22]），且每个部分恰好
    含一段数字。通过则把各部分的字符区间收缩到其数字段并打簇组号；
    注入时要求整组解析成功且落同一块，否则整组放弃。
    """
    by_page: dict[int, list[_Link]] = {}
    for lk in links:
        if not lk.dest_name.startswith("cite."):
            continue
        if _CLUSTER_FULL_RE.match(lk.text.strip()):
            continue  # 完整引文本就可注入，不是部分矩形
        if not _CLUSTER_PART_RE.match(lk.text.strip()):
            continue
        by_page.setdefault(lk.page, []).append(lk)

    gid = 0
    for pno, cands in by_page.items():
        page = pages[pno]
        if page is None:
            continue
        geo = []
        for lk in cands:
            r = _link_rect(page, lk)
            if r is not None:
                geo.append((r, lk))
        # 按行心 y、x0 排序后连成 run：y 差超阈值或 x 间隙超限即断开
        geo.sort(key=lambda t: ((t[0][1] + t[0][3]) / 2, t[0][0]))
        runs: list[list[tuple]] = []
        for r, lk in geo:
            if runs:
                pr, _pl = runs[-1][-1]
                y_gap = abs((r[1] + r[3]) / 2 - (pr[1] + pr[3]) / 2)
                x_gap = r[0] - pr[2]
                if y_gap <= _CLUSTER_Y_TOL_PT and \
                        -_CLUSTER_X_OVERLAP_PT <= x_gap <= _CLUSTER_X_GAP_PT:
                    runs[-1].append((r, lk))
                    continue
            runs.append([(r, lk)])
        for run in runs:
            if len(run) < 2:
                continue
            parts = [lk for _r, lk in run]
            full = page.raw[parts[0].c0:parts[-1].c1].strip()
            if not _CLUSTER_FULL_RE.match(full):
                continue
            # 每个部分恰含一段数字；收缩区间到数字段
            shrunk = []
            for lk in parts:
                digits = [i for i in range(lk.c0, lk.c1)
                          if i < len(page.raw) and page.raw[i].isdigit()]
                if not digits:
                    shrunk = None
                    break
                d0, d1 = digits[0], digits[-1] + 1
                seg = page.raw[d0:d1]
                if not seg.isdigit():
                    shrunk = None  # 数字被逗号/横线断开 → 一个部分含两段数字，放弃
                    break
                shrunk.append((lk, d0, d1, seg))
            if shrunk is None:
                continue
            for lk, d0, d1, seg in shrunk:
                lk.c0, lk.c1 = d0, d1
                lk.text = seg
                lk.cluster = gid
            gid += 1
    return links


def _parse_dest_string(dest: str, page_height: float) -> tuple[float, float] | None:
    """解析 named dest 的目标串（'/FitR 0 446 596 437'、'/XYZ 32 748 0' 等），
    返回 fitz 坐标系（左上原点）下的目标点；不支持的形式 → None。

    PDF dest 串是左下原点用户空间坐标，y 需按页高翻转。
    """
    try:
        parts = dest.split()
        kind = parts[0]
        vals = [float(v) for v in parts[1:] if re.match(r"^-?[\d.]+$", v)]
        if kind == "/XYZ" and len(vals) >= 2:
            return vals[0], page_height - vals[1]
        if kind == "/FitH" and vals:
            return 0.0, page_height - vals[0]
        if kind == "/FitR" and len(vals) >= 4:
            return (vals[0] + vals[2]) / 2, page_height - (vals[1] + vals[3]) / 2
    except (ValueError, IndexError):
        pass
    return None  # /Fit /FitB /FitBH 等无确定目标点，交由同号唯一兜底


def _split_math_core(core: str, label: str) -> str | None:
    """证据驱动剥离的核心判定：数学段芯 core 以"编号 + 尾部闭括号/空白"收尾时
    拆出编号，返回剥离后的段芯；否则 None。

    判定（全满足才拆，绝不凭猜）：段芯去掉尾部 }/空白后以 label 数字收尾；
    剥离后余部非空、以数字或 } 结尾、花括号仍平衡（如 'f^{-4/32}' →
    'f^{-4/3}'；'$x^2$' 的芯 'x^2' 剥掉 2 余 'x^' 以 ^ 结尾 → 不拆）。
    """
    m = re.search(r"[\}\s]*$", core)
    body, tail = core[:m.start()], core[m.start():]
    if not body.endswith(label):
        return None
    rest_body = body[:-len(label)]
    if not rest_body or rest_body[-1] not in "0123456789}":
        return None
    rest = rest_body + tail
    bare = rest.replace("\\{", "").replace("\\}", "")
    if bare.count("{") != bare.count("}"):
        return None
    return rest


def _find_swallowed_marker(lk: "_Link", label: str, blocks: list,
                           aligner: "_Aligner"):
    """证据驱动剥离的落点找回（forecast eq12：上标脚注标记被引擎吞进数学段
    尾部，f^{-4/3}² → 块内 $f^{-4/32}$，标记在等值段外无法对齐）。

    双证据：①块的对齐文本（等值段）终点紧邻链接标记（页骨架坐标差 ≤3）；
    ②该终点落在某数学段内，且段芯以编号 label 收尾、剥掉后余部非空、
    以数字或 } 结尾、花括号仍平衡。返回 (块下标, 段起, 段止, 剥离后余部)，
    任一条件不满足 → None（绝不凭猜拆数学）。
    """
    page = aligner.pages[lk.page]
    if page is None:
        return None
    s0 = _raw_to_skel(page, lk.c0, forward=True)
    if s0 is None:
        return None
    for idx, b in enumerate(blocks):
        if b.kind not in _INJECTABLE_KINDS:
            continue
        if getattr(b, "src_page", None) not in (lk.page, lk.page - 1):
            continue
        bm = aligner.block_map(idx)
        if bm is None:
            continue
        if lk.page == bm.frame_page:
            f0 = s0
        elif bm.page1_off is not None and lk.page == bm.frame_page + 1:
            f0 = bm.page1_off + s0
        else:
            continue
        for a, b_, sz in bm.segs:
            end_f = a + sz          # 等值段页侧终点（不含）
            if not (0 <= f0 - end_f <= 3):
                continue
            end_b = b_ + sz - 1     # 等值段块侧末字符
            if not (0 <= end_b < len(bm.b2o)):
                continue
            bo = bm.b2o[end_b]
            for m in _MATH_SPAN_RE.finditer(b.content or ""):
                if not (m.start() <= bo < m.end()):
                    continue
                rest = _split_math_core(m.group(0)[1:-1], label)
                if rest is not None:
                    return idx, m.start(), m.end(), rest
    return None


def _place_unique_citation(lk: _Link, num: str, blocks: list,
                           math_spans: dict):
    """上标引文兜底落位：源页候选块内 "[num]" 恰好唯一出现 → (块下标, o0, o1)；
    零次/多次/命中数学段 → None（维持放弃）。"""
    needle = f"[{num}]"
    hits = []
    for idx, b in enumerate(blocks):
        if b.kind not in _INJECTABLE_KINDS:
            continue
        if getattr(b, "src_page", None) not in (lk.page, lk.page - 1):
            continue
        start = 0
        while True:
            j = (b.content or "").find(needle, start)
            if j < 0:
                break
            hits.append((idx, j, j + len(needle)))
            start = j + 1
    if len(hits) != 1:
        return None
    idx, o0, o1 = hits[0]
    spans = math_spans.get(idx)
    if spans is None:
        spans = [(m.start(), m.end())
                 for m in _MATH_SPAN_RE.finditer(blocks[idx].content)]
        math_spans[idx] = spans
    if any(o0 < e and o1 > s for s, e in spans):
        return None
    return idx, o0, o1


def extract_pdf_links(pdf_path) -> tuple[list, list] | None:
    """提取 PDF 链接注释。返回 (pages, links)；无链接注释/打开失败 → None。

    pages: list[_Page | None]（只为有链接源/目标的页建字符流）；
    links: list[_Link]（源矩形 raw 字符区间 + 目标）。
    """
    import fitz

    try:
        doc = fitz.open(str(pdf_path))
    except Exception as e:
        logger.warning(f"  链接提取：PDF 打开失败（{e}）")
        return None
    try:
        try:
            names = doc.resolve_names() or {}
        except Exception:
            names = {}
        pages: list[_Page | None] = [None] * doc.page_count
        links: list[_Link] = []
        seen = set()
        for pno in range(doc.page_count):
            fitz_page = doc[pno]
            if not fitz_page.get_links():
                continue
            page = _Page(fitz_page)
            pages[pno] = page
            for lk in fitz_page.get_links():
                kind = lk.get("kind")
                uri = dest_name = ""
                dest_page, dest_x, dest_y = -1, 0.0, 0.0
                if kind == fitz.LINK_URI:
                    uri = (lk.get("uri") or "").strip()
                    if not uri:
                        continue
                elif kind in (fitz.LINK_GOTO, fitz.LINK_NAMED):
                    dest_name = lk.get("nameddest", "") or ""
                    # MuPDF 的 LINK_NAMED page 字段可能是字符串（martins2000
                    # 实测 '10'，直接比较即 TypeError 全灭）——先归一成 int
                    try:
                        dest_page = int(lk.get("page", -1))
                    except (TypeError, ValueError):
                        dest_page = -1
                    to = lk.get("to")
                    if to is not None:
                        dest_x, dest_y = to.x, to.y
                        if kind == fitz.LINK_NAMED \
                                and 0 <= dest_page < doc.page_count:
                            # named dest 的 to 是 PDF 用户空间（左下原点）y 坐标
                            # （MuPDF 不做翻转——cosmic A4 与 forecast Letter 实测；
                            # LINK_GOTO 的 to 已是 fitz 顶左坐标，不在此列）
                            dest_y = doc[dest_page].rect.height - dest_y
                    if kind == fitz.LINK_NAMED and dest_name \
                            and (dest_page < 0 or to is None):
                        # 页码或坐标缺失 → resolve_names() 补（书签式 dest 的
                        # '/FitR x0 y0 x1 y1' 串也解析出目标点）；页码仍缺则放弃
                        nm = names.get(dest_name)
                        if nm:
                            if dest_page < 0:
                                try:
                                    dest_page = int(nm.get("page", -1))
                                except (TypeError, ValueError):
                                    dest_page = -1
                            if to is None:
                                to2 = nm.get("to")
                                if to2 is not None and 0 <= dest_page < doc.page_count:
                                    # resolve_names 的 to 同样是左下原点 y
                                    dest_x = to2[0] if not hasattr(to2, "x") else to2.x
                                    raw_y = to2[1] if not hasattr(to2, "y") else to2.y
                                    dest_y = doc[dest_page].rect.height - raw_y
                                elif 0 <= dest_page < doc.page_count:
                                    pt = _parse_dest_string(
                                        nm.get("dest") or "",
                                        doc[dest_page].rect.height)
                                    if pt is not None:
                                        dest_x, dest_y = pt
                        if dest_page < 0:
                            continue  # 页码解析失败，放弃该条（保纯文本）
                    if not (0 <= dest_page < doc.page_count):
                        continue
                else:
                    continue  # LINK_LAUNCH 等类型不支持

                rect = lk.get("from")
                if rect is None or rect.is_empty:
                    continue
                span = _span_for_rect(page, rect)
                if span is None:
                    continue
                c0, c1 = span
                text = page.raw[c0:c1].strip()
                if not text or len(text) > _MAX_LINK_TEXT:
                    continue
                key = (pno, c0, c1, uri or dest_name or (dest_page, round(dest_y, 1)))
                if key in seen:
                    continue  # 同一矩形叠了多个相同注释（出版商冗余）
                seen.add(key)
                links.append(_Link(pno, c0, c1, text, uri,
                                   dest_page, dest_x, dest_y, dest_name))
        if not links:
            # 可分辨的零产出原因：页面无任何链接注释 vs 有注释但全部不可用
            total_ann = sum(len(doc[p].get_links()) for p in range(doc.page_count))
            if total_ann:
                logger.info(f"  链接提取: PDF 共 {total_ann} 条链接注释，"
                            "无可用条目（全为不支持类型/解析失败）")
            else:
                logger.info("  链接提取: PDF 无链接注释（扫描版/无注解），跳过")
            return None
        # 同 URI 且区间相邻（仅隔空白/换行）的链接矩形合并——跨行拆链
        # （LaTeX hyperref 常把一个 URL 拆成多个注释），合并后白名单校验
        # 与括号平衡检查才能看到完整显示文字
        merged: list[_Link] = []
        for lk in sorted(links, key=lambda l: (l.page, l.c0, l.c1)):
            if (lk.uri and merged and merged[-1].uri == lk.uri
                    and merged[-1].page == lk.page
                    and not pages[lk.page].raw[merged[-1].c1:lk.c0].strip()):
                prev = merged[-1]
                prev.c1 = lk.c1
                prev.text = pages[lk.page].raw[prev.c0:prev.c1].strip()
            else:
                merged.append(lk)
        links = merged
        # 目标页可能没有链接源（如参考文献页），按需补建字符流
        for pno in {lk.dest_page for lk in links if lk.dest_page >= 0}:
            if pages[pno] is None:
                pages[pno] = _Page(doc[pno])
        return pages, links
    finally:
        doc.close()


# ============================================================
# 对齐：最终块文本 ↔ 页骨架（difflib 等值段给出字符级映射）
# ============================================================

# 可注入链接的块类型（heading 不注入——标题内链接会破坏 TOC/slug 稳定性；
# equation 整个跳过；table 的 HTML 表体不注入；footnote 可注入 URI 邮箱等）
_INJECTABLE_KINDS = {"paragraph", "reference", "image", "table_image", "footnote"}
# 参与目标覆盖的块类型（image/table 用图注/表注文本对齐；equation 的 LaTeX
# 内容与 PDF 原文差异大，位置覆盖常落空，靠同号唯一 \tag 兜底）
_TARGETABLE_KINDS = _INJECTABLE_KINDS | {"heading", "table", "equation"}


class _BlockMap:
    """一个最终块与其源页（含下一页，跨页合并段）骨架的对齐结果。"""

    __slots__ = ("idx", "frame_page", "frame_skel", "page1_off", "segs", "bskel", "b2o")

    def __init__(self, idx, frame_page, frame_skel, page1_off, segs, bskel, b2o):
        self.idx = idx
        self.frame_page = frame_page      # 对齐坐标系的起始页
        self.frame_skel = frame_skel
        self.page1_off = page1_off        # 第二页在坐标系中的起始下标（无则 None）
        self.segs = segs                  # [(frame_start, block_skel_start, size)] 等值段
        self.bskel = bskel
        self.b2o = b2o                    # 块骨架下标 → 块原文下标


class _Aligner:
    """页骨架与最终块的对齐器（按需构建，块级缓存）。"""

    def __init__(self, pages: list, blocks: list):
        self.pages = pages
        self.blocks = blocks
        self._frames: dict[int, tuple[str, int | None]] = {}
        self._maps: dict[int, _BlockMap | None] = {}

    def _frame(self, p0: int) -> tuple[str, int | None]:
        """页 p0（拼接 p0+1，覆盖跨页合并段）的骨架坐标系。"""
        fr = self._frames.get(p0)
        if fr is not None:
            return fr
        s0 = self.pages[p0].skel if 0 <= p0 < len(self.pages) and self.pages[p0] else ""
        p1 = p0 + 1
        if p1 < len(self.pages) and self.pages[p1] is not None and self.pages[p1].skel:
            frame = s0 + " " + self.pages[p1].skel
            off1 = len(s0) + 1
        else:
            frame, off1 = s0, None
        self._frames[p0] = (frame, off1)
        return frame, off1

    def block_map(self, idx: int) -> _BlockMap | None:
        """块 ↔ 页面对齐（difflib 等值段）。无源页信息/完全对不上 → None。"""
        if idx in self._maps:
            return self._maps[idx]
        bm = self._build(idx)
        self._maps[idx] = bm
        return bm

    def _build(self, idx: int) -> _BlockMap | None:
        b = self.blocks[idx]
        p0 = getattr(b, "src_page", None)
        if p0 is None or not (0 <= p0 < len(self.pages)) or self.pages[p0] is None:
            return None
        text = b.caption if b.kind == "table" else (b.content or "")
        if not text or not text.strip():
            return None
        bskel, b2o = _skeletonize(text)
        if not bskel:
            return None
        frame, off1 = self._frame(p0)
        if not frame:
            return None
        sm = difflib.SequenceMatcher(None, frame, bskel, autojunk=False)
        segs = [(i1, j1, i2 - i1) for tag, i1, i2, j1, _j2 in sm.get_opcodes()
                if tag == "equal" and i2 > i1]
        if not segs:
            return None
        return _BlockMap(idx, p0, frame, off1, segs, bskel, b2o)

    def map_span(self, bm: _BlockMap, link: "_Link",
                 strict: bool = True) -> tuple[int, int] | None:
        """链接源字符区间 → 块原文偏移 [o0, o1)。不在同一等值段内 → None。

        strict 置信度：等值段须比区间长出上下文（≥4 字符），或触及块首/尾
        （孤立的 3 字符巧合匹配不足为凭）。strict=False 仅供脚注上标标记的
        $^{N}$ 归一形态特判（后续还有段内文字逐字校验兜底）。
        """
        page = self.pages[link.page]
        s0 = _raw_to_skel(page, link.c0, forward=True)
        s1 = _raw_to_skel(page, link.c1 - 1, forward=False)
        if s0 is None or s1 is None or s1 < s0:
            return None
        s1 += 1
        if link.page == bm.frame_page:
            f0, f1 = s0, s1
        elif bm.page1_off is not None and link.page == bm.frame_page + 1:
            f0, f1 = bm.page1_off + s0, bm.page1_off + s1
        else:
            return None
        for a, b_, sz in bm.segs:
            if a <= f0 and f1 <= a + sz:
                o0 = bm.b2o[b_ + (f0 - a)]
                o1 = bm.b2o[b_ + (f1 - 1 - a)] + 1
                context = sz - (f1 - f0)
                touches_edge = (b_ == 0) or (b_ + sz == len(bm.bskel))
                if strict and context < 4 and not touches_edge:
                    return None  # 孤立巧合匹配，置信度不足
                return o0, o1
        return None

    def covering_block(self, page_no: int, y: float, x: float) -> int | None:
        """目标点 (page, x, y) → 覆盖该行的块下标（最近 3 行内找覆盖者）。"""
        page = self.pages[page_no]
        if page is None:
            return None
        ranked = sorted(
            page.lines,
            key=lambda ln: (0.0 if ln[2].y0 <= y <= ln[2].y1
                            else min(abs(y - ln[2].y0), abs(y - ln[2].y1)),
                            0.0 if ln[2].x0 <= x <= ln[2].x1
                            else min(abs(x - ln[2].x0), abs(x - ln[2].x1))))
        for s0, s1, _rect in ranked[:3]:
            best_idx, best_ov = None, 0
            for idx, b in enumerate(self.blocks):
                if b.kind not in _TARGETABLE_KINDS:
                    continue
                sp = getattr(b, "src_page", None)
                if sp not in (page_no, page_no - 1):
                    continue
                bm = self.block_map(idx)
                if bm is None:
                    continue
                if bm.frame_page == page_no:
                    f0, f1 = s0, s1
                elif bm.page1_off is not None and bm.frame_page + 1 == page_no:
                    f0, f1 = bm.page1_off + s0, bm.page1_off + s1
                else:
                    continue
                ov = max(0, sum(min(f1, a + sz) - max(f0, a)
                                for a, _b, sz in bm.segs
                                if min(f1, a + sz) > max(f0, a)))
                if ov > best_ov:
                    best_idx, best_ov = idx, ov
            if best_idx is not None and best_ov >= 3:
                return best_idx
        return None


# ============================================================
# 注入主流程
# ============================================================

def collect_paper_links(blocks: list, source_pdf) -> LinkResult | None:
    """提取 source_pdf 的原生链接并注入 blocks（就地修改 content）。

    返回 LinkResult（anchors 为 renderer 需发射的锚点 id 集合）；
    PDF 无链接注释/不可读 → None（调用方保持零副作用输出）。
    不确定的链接一律放弃保留纯文本，禁止编造目标。
    """
    extracted = extract_pdf_links(source_pdf)
    if extracted is None:
        return None
    pages, links = extracted
    # 引文簇/区间部分矩形合并（[2, 3] → "[2,"+"3]" 等）：通过校验的部分
    # 收缩到数字段并打簇组号，主循环结束后整组同块判定提交
    links = _merge_citation_clusters(links, pages)
    aligner = _Aligner(pages, blocks)

    # 目标映射用的索引：编号 → 块下标（同号多块 → 歧义，回退不可用）
    ref_index: dict[str, list[int]] = {}
    fig_index: dict[str, list[int]] = {}
    tab_index: dict[str, list[int]] = {}
    eq_index: dict[str, list[int]] = {}
    head_index: dict[str, list[int]] = {}
    for idx, b in enumerate(blocks):
        aid = block_anchor_id(b)
        if not aid:
            continue
        kind, _, num = aid.partition("-")
        idx_map = {"ref": ref_index, "fig": fig_index, "tab": tab_index,
                   "eq": eq_index, "sec": head_index}.get(kind)
        if idx_map is None:
            continue
        if kind == "sec":
            m = _HEAD_NUM_RE.match(b.content or "")
            if m:
                head_index.setdefault(m.group(1).upper(), []).append(idx)
        else:
            idx_map.setdefault(num, []).append(idx)

    # 每个块待注入区间（块下标 → [(o0, o1, target, cat)]）
    injections: dict[int, list[tuple[int, int, str, str]]] = {}
    math_spans: dict[int, list[tuple[int, int]]] = {}
    cluster_pending: dict[int, list[tuple[tuple | None, str | None, str]]] = {}
    result = LinkResult()

    def _stat(cat: str, ok: bool):
        slot = result.stats.setdefault(cat, [0, 0])
        slot[0 if ok else 1] += 1
        if ok:
            result.injected += 1
        else:
            result.dropped += 1

    def _target_block(lk: _Link) -> int | None:
        if lk.dest_page < 0 or pages[lk.dest_page] is None:
            return None
        return aligner.covering_block(lk.dest_page, lk.dest_y, lk.dest_x)

    def _resolve(lk: _Link, placed_kind: str | None) -> tuple[str | None, str]:
        """目标映射 + 编号一致性校验。返回 (锚点/URI 或 None, 类别)。"""
        text = lk.text.strip()
        if lk.uri:
            # URI：白名单收可见文字本身是 URL/DOI/邮箱的；
            # 落在参考文献条目内的期刊/DOI 超链（可见文字是刊名卷期页）也收
            t = text.strip(_URI_TRIM_HEAD + _URI_TRIM_TAIL)
            if _URI_TEXT_RE.search(t) or (lk.uri.startswith("mailto:") and "@" in t):
                return lk.uri, "uri"
            if placed_kind == "reference":
                return lk.uri, "uri"
            return None, "uri"

        dest = lk.dest_name
        m_cite = _CITE_TEXT_RE.match(text)
        if dest.startswith("cite."):
            # 引文链接：编号取自源文字；位置映射与编号必须一致，
            # 失配时回退"同号唯一条目"（cite 目标块本就按编号锚定）
            if not m_cite:
                return None, "ref"
            num = m_cite.group(1)
            idx = _target_block(lk)
            if idx is not None and blocks[idx].kind == "reference" \
                    and block_anchor_id(blocks[idx]) == f"ref-{num}":
                return f"#ref-{num}", "ref"
            cands = ref_index.get(num, [])
            if len(cands) == 1:
                return f"#ref-{num}", "ref"
            return None, "ref"
        if _FN_DEST_RE.search(dest):
            # 脚注链接：目标映射须落 footnote 块。编号脚注：编号取自目标块
            # 解析出的 note_num（Hfootnote.N 等 dest 是内部计数器，与印刷
            # 编号可能错一——forecast 实测 Hfootnote.2 的可见标记是 '1'），
            # 源标记数字与 note_num 必须一致。符号脚注（∗/† 作者邮箱类）：
            # 源符号与脚注块首标记同符才链（label 用源可见字符）。
            idx = _target_block(lk)
            if idx is None or blocks[idx].kind != "footnote":
                return None, "fn"
            fb = blocks[idx]
            n = getattr(fb, "note_num", None)
            dm = re.search(r"\d+", text)
            if n is not None:
                if dm is None or int(dm.group(0)) != n:
                    return None, "fn"  # 编号对不上 → 放弃
                return f"fn:{n}", "fn"
            sym = text.strip(".,;:· ")
            if n is None and len(sym) == 1 and sym in _FN_SYM_MAP \
                    and _footnote_symbol_of(fb) == _FN_SYM_MAP[sym]:
                lk.fn_target = idx  # label 延迟到注入真正写入时落块（防孤儿）
                return f"fn:{sym}", "fn"
            return None, "fn"
        if dest.startswith("equation"):
            # equation.N 的 N 是 hyperref 内部计数器而非印刷 \tag 编号
            # （实测 equation.4 的可见文字是 (5)），编号取自源文字，
            # 位置映射与编号必须一致，失配回退"同号唯一 \tag 块"
            em = _EQ_NUM_DEST_RE.match(text)
            if not em:
                return None, "eq"
            num = em.group(1)
            idx = _target_block(lk)
            if idx is not None and blocks[idx].kind == "equation" \
                    and block_anchor_id(blocks[idx]) == f"eq-{num}":
                return f"#eq-{num}", "eq"
            cands = eq_index.get(num, [])
            if len(cands) == 1:
                return f"#eq-{num}", "eq"
            return None, "eq"
        if dest.startswith("figure."):
            num = dest.split(".", 1)[1]
            tm = _NUM_TEXT_RE.search(text)
            if tm and tm.group(1) != num:
                return None, "fig"  # 源文字编号与 dest 声明矛盾，放弃
            idx = _target_block(lk)
            if idx is not None and block_anchor_id(blocks[idx]) == f"fig-{num}":
                return f"#fig-{num}", "fig"
            cands = fig_index.get(num, [])
            if len(cands) == 1:
                return f"#fig-{num}", "fig"
            return None, "fig"
        if dest.startswith("table."):
            num = dest.split(".", 1)[1]
            tm = _NUM_TEXT_RE.search(text)
            if tm and tm.group(1) != num:
                return None, "tab"
            idx = _target_block(lk)
            if idx is not None and block_anchor_id(blocks[idx]) == f"tab-{num}":
                return f"#tab-{num}", "tab"
            cands = tab_index.get(num, [])
            if len(cands) == 1:
                return f"#tab-{num}", "tab"
            return None, "tab"
        if dest.startswith("section"):
            sm_ = _SEC_TEXT_RE.match(text)
            if not sm_:
                return None, "sec"
            num = sm_.group(1).upper()
            idx = _target_block(lk)
            if idx is not None and blocks[idx].kind == "heading":
                hm = _HEAD_NUM_RE.match(blocks[idx].content or "")
                if hm and hm.group(1).upper() == num:
                    return "#" + block_anchor_id(blocks[idx]), "sec"
            cands = head_index.get(num, [])
            if len(cands) == 1:
                return "#" + block_anchor_id(blocks[cands[0]]), "sec"
            return None, "sec"
        if dest:
            # 出版商书签式 dest（Elsevier/RSC 等）：词干定类别、尾部编号即
            # 印刷编号（与 hyperref 计数器不同，可与源文字编号交叉校验）
            m = _PUB_DEST_RE.search(dest)
            if not m:
                return None, "other"  # 未识别的 named dest 类型，不猜
            cat = _PUB_DEST_CAT[m.group(1).lower()]
            num = m.group(2)
            tm = _NUM_TEXT_RE.search(text)
            if tm is None or tm.group(1) != num:
                return None, cat  # 源文字编号与 dest 编号对不上 → 放弃
            idx = _target_block(lk)
            if idx is not None and block_anchor_id(blocks[idx]) == f"{cat}-{num}":
                return f"#{cat}-{num}", cat
            cands = {"ref": ref_index, "fig": fig_index,
                     "tab": tab_index, "eq": eq_index}[cat].get(num, [])
            if len(cands) == 1:
                return f"#{cat}-{num}", cat
            return None, cat

        # LINK_GOTO（无 nameddest）：只能靠源文字形态分类 + 位置映射校验
        if m_cite and text.startswith("["):
            num = m_cite.group(1)
            idx = _target_block(lk)
            if idx is not None and blocks[idx].kind == "reference" \
                    and block_anchor_id(blocks[idx]) == f"ref-{num}":
                return f"#ref-{num}", "ref"
            return None, "ref"
        # 无名 named dest 的裸数字引文（arXiv 老 PDF，martins2000 实测：kind=NAMED
        # 但 nameddest 为空、只有目标页没有坐标）：编号 + 目标页即条目页双校验。
        # 锚点页与条目页允许 ±1 偏差（该文 dest 页比条目实际页多 1——hyperref
        # 把文献锚打在条目流的次页起首，实测）
        if m_cite:
            num = m_cite.group(1)
            cands = ref_index.get(num, [])
            if len(cands) == 1 and lk.dest_page >= 0:
                sp = getattr(blocks[cands[0]], "src_page", None)
                if sp is not None and abs(sp - lk.dest_page) <= 1:
                    return f"#ref-{num}", "ref"
            return None, "ref"
        fm = _FIG_TEXT_RE.match(text)
        if fm:
            num = fm.group(1)
            idx = _target_block(lk)
            if idx is not None and block_anchor_id(blocks[idx]) == f"fig-{num}":
                return f"#fig-{num}", "fig"
            cands = fig_index.get(num, [])
            if len(cands) == 1:
                return f"#fig-{num}", "fig"
            return None, "fig"
        tm = _TAB_TEXT_RE.match(text)
        if tm:
            num = tm.group(1)
            idx = _target_block(lk)
            if idx is not None and block_anchor_id(blocks[idx]) == f"tab-{num}":
                return f"#tab-{num}", "tab"
            cands = tab_index.get(num, [])
            if len(cands) == 1:
                return f"#tab-{num}", "tab"
            return None, "tab"
        # 公式引用："Eq. (5)" 文字自带语义，允许同号唯一兜底；
        # 裸 "(5)" 只信位置映射（与裸 "[12]" 同规）
        em = _EQ_NUM_TEXT_RE.match(text)
        if em:
            num = em.group(1)
            idx = _target_block(lk)
            if idx is not None and blocks[idx].kind == "equation" \
                    and block_anchor_id(blocks[idx]) == f"eq-{num}":
                return f"#eq-{num}", "eq"
            if not _EQ_NUM_BARE_RE.match(text):  # "Eq. (5)" 形态
                cands = eq_index.get(num, [])
                if len(cands) == 1:
                    return f"#eq-{num}", "eq"
            return None, "eq"
        sm_ = _SEC_TEXT_RE.match(text)
        if sm_ and re.match(r"^(?:Section|Sec|§)", text, re.I):
            num = sm_.group(1).upper()
            idx = _target_block(lk)
            if idx is not None and blocks[idx].kind == "heading":
                hm = _HEAD_NUM_RE.match(blocks[idx].content or "")
                if hm and hm.group(1).upper() == num:
                    return "#" + block_anchor_id(blocks[idx]), "sec"
            cands = head_index.get(num, [])
            if len(cands) == 1:
                return "#" + block_anchor_id(blocks[cands[0]]), "sec"
        return None, "other"

    for lk in links:
        # URI 链接先裁掉源区间首尾的括号字符（显示文字更干净，括号留在链接外）
        if lk.uri:
            while lk.c0 < lk.c1 and pages[lk.page].raw[lk.c0] in _URI_TRIM_HEAD:
                lk.c0 += 1
            while lk.c1 > lk.c0 and pages[lk.page].raw[lk.c1 - 1] in _URI_TRIM_TAIL:
                lk.c1 -= 1
            if lk.c1 - lk.c0 < 4:
                _stat("uri", False)
                continue
        # 源区间 → 所属块 + 块内偏移（字符级对齐 + 区间守卫 + 数学段跳过）
        placed: tuple[int, int, int] | None = None
        math_hit: tuple[int, int, int] | None = None  # 位置已明但落在数学段内
        for idx, b in enumerate(blocks):
            if b.kind not in _INJECTABLE_KINDS:
                continue
            if getattr(b, "src_page", None) not in (lk.page, lk.page - 1):
                continue
            bm = aligner.block_map(idx)
            if bm is None:
                continue
            offsets = aligner.map_span(bm, lk)
            if offsets is None:
                # 严格对齐失败：宽松档仅供脚注上标标记（$^{N}$ 数学段归一
                # 形态）特判——区间守卫照过，且必须落在数学段内
                loose = aligner.map_span(bm, lk, strict=False)
                if loose is not None:
                    o0, o1 = loose
                    if _skeletonize(b.content[o0:o1])[0] == _skeletonize(
                            pages[lk.page].raw[lk.c0:lk.c1])[0]:
                        spans = math_spans.get(idx)
                        if spans is None:
                            spans = [(m.start(), m.end())
                                     for m in _MATH_SPAN_RE.finditer(b.content)]
                            math_spans[idx] = spans
                        if any(o0 >= s and o1 <= e for s, e in spans):
                            math_hit = (idx, o0, o1)
                            break
                continue
            o0, o1 = offsets
            # 区间守卫：块内子串骨架须与链接源文字一致（防错位注入）
            sub_sk = _skeletonize(b.content[o0:o1])[0]
            want_sk = _skeletonize(
                pages[lk.page].raw[lk.c0:lk.c1])[0]
            if sub_sk != want_sk:
                continue
            # 数学段内不注入（脚注标记的 $^{N}$ 归一形态在解析后特判）
            spans = math_spans.get(idx)
            if spans is None:
                spans = [(m.start(), m.end())
                         for m in _MATH_SPAN_RE.finditer(b.content)]
                math_spans[idx] = spans
            if any(o0 < e and o1 > s for s, e in spans):
                math_hit = (idx, o0, o1)
                break
            placed = (idx, o0, o1)
            break
        # 目标映射 + 校验（URI 白名单兜底要看落在哪种块里）
        target, cat = _resolve(lk, blocks[placed[0]].kind if placed else None)
        fn_whole_math = False
        fn_replacement = None
        if placed is None and math_hit is not None and target is not None \
                and target.startswith("fn:"):
            # 脚注标记被引擎归一为 $^{N}$ 数学段：整段替换为 [^N]
            # （上标渲染形态等价；段内文字与 label 逐字一致才动，零丢失）
            label = target[3:]
            bidx, o0, o1 = math_hit
            content = blocks[bidx].content
            for m in _MATH_SPAN_RE.finditer(content):
                if m.start() <= o0 and o1 <= m.end():
                    inner = re.fullmatch(r"\$\^\{?([^\s{}$]+)\}?\$", m.group(0))
                    if inner and inner.group(1) == label:
                        placed = (bidx, m.start(), m.end())
                        fn_whole_math = True
                    elif label.isdigit():
                        # 证据驱动剥离（forecast eq12 实测）：脚注链接注释精确
                        # 覆盖的上标数字被引擎吞进数学段尾部（f^{-4/3}² →
                        # f^{-4/32}$）。仅当：fn 目标已验证 + 落点在数学段内 +
                        # 段芯以编号+闭括号收尾 + 剥离后形态合法——才拆出
                        rest = _split_math_core(m.group(0)[1:-1], label)
                        if rest is not None:
                            placed = (bidx, m.start(), m.end())
                            fn_whole_math = True
                            fn_replacement = f"${rest}$[^{label}]"
                    break
        if target is not None and placed is not None and target.startswith("fn:") \
                and not fn_whole_math:
            # 脚注引用点：标记字符移入 label（替换式注入）。区间收缩到数字段
            # （编号脚注，矩形常把句读一并覆盖）或要求恰为符号字符；
            # 收缩后块内子串 == label，剥除 label 即还原（零丢失）
            bidx, o0, o1 = placed
            sub = blocks[bidx].content[o0:o1]
            label = target[3:]
            if label.isdigit():
                dm = re.search(r"\d+", sub)
                if dm is None or dm.group(0) != label:
                    target = None
                else:
                    o0 += dm.start()
                    o1 = o0 + len(dm.group(0))
                    placed = (bidx, o0, o1)
            elif sub != label:
                target = None  # 符号标记无法字符级对齐 → 放弃（定义保持原样）
        if placed is None and target is not None and target.startswith("fn:") \
                and target[3:].isdigit():
            # 证据驱动剥离的落点找回：标记被吞进数学尾部时它在块内对不齐
            # （等值段外），改用"对齐文本终点紧邻标记 + 数学段芯以编号收尾"
            # 双证据定位（forecast eq12 f^{-4/32} 实测形态）
            label = target[3:]
            hit = _find_swallowed_marker(lk, label, blocks, aligner)
            if hit is not None:
                bidx, m_start, m_end, rest = hit
                placed = (bidx, m_start, m_end)
                fn_whole_math = True
                fn_replacement = f"${rest}$[^{label}]"
        if placed is None and target is not None and cat == "ref" \
                and (lk.dest_name.startswith("cite.")
                     or bool(_PUB_DEST_RE.search(lk.dest_name or ""))):
            # 上标引文兜底落位：RSC 式上标数字引文，PDF 是裸数字上标而块内
            # "[N]" 的括号是引擎归一产物，字符级对齐必然失败。此时目标已由
            # 原生 dest 确定（非编造），只需在源页候选块里找 "[N]" 的
            # 唯一出现位置落位；多义（同页多处 [N]）维持放弃
            num = target.split("-", 1)[1]
            placed = _place_unique_citation(lk, num, blocks, math_spans)
        if lk.cluster >= 0:
            # 簇部分矩形：暂缓提交，主循环后整组判定（全解析成功且落同一块）
            cluster_pending.setdefault(lk.cluster, []).append((placed, target, cat))
            continue
        if target is not None and placed is not None:
            # 元组定长 6 元：[o0, o1, target, cat, fn_target, replacement]
            # （编号脚注不走符号 label 落块；replacement 仅证据驱动剥离用）
            inj = [placed[1], placed[2], target, cat,
                   lk.fn_target if target.startswith("fn:") else -1,
                   fn_replacement]
            injections.setdefault(placed[0], []).append(inj)
            _stat(cat, True)
        else:
            _stat(cat, False)

    # 簇整组提交：任一成员未解析/未落块、或成员散落不同块 → 整组放弃
    # （部分矩形本就过不了单条校验，放弃即维持原纯文本，不损失信息）
    for _gid, items in cluster_pending.items():
        idxs = {p[0] for p, t, _c in items if p is not None and t is not None}
        ok = (len(idxs) == 1
              and all(p is not None and t is not None for p, t, _c in items))
        for p, t, cat in items:
            if ok:
                injections.setdefault(p[0], []).append((p[1], p[2], t, cat))
            _stat(cat, ok)

    # 应用注入：同块多区间按偏移倒序逐个拼接，互不干扰。
    # 先就地合并同目标的相邻区间（跨行链接常被拆成多个矩形，如
    # "arXiv:xxx [astro-" + "ph.CO]"，合并后显示文字方括号恢复平衡）；
    # 合并后仍不平衡的放弃（不平衡括号会破坏 Markdown 链接语法与 A/B 剥离）。
    # fn: 目标是脚注引用点——标记字符替换为 [^label]（不包链接语法）
    for idx, spans in injections.items():
        b = blocks[idx]
        merged: list[list] = []
        for inj in sorted(spans):
            o0, o1, target = inj[0], inj[1], inj[2]
            if merged and merged[-1][2] == target and merged[-1][1] == o0 \
                    and not target.startswith("fn:"):
                merged[-1][1] = o1  # 紧邻同目标：延伸前一区间
            else:
                merged.append(inj)
        content = b.content
        last_start = len(content) + 1
        for inj in reversed(merged):
            o0, o1, target, cat = inj[0], inj[1], inj[2], inj[3]
            if o1 > last_start or not _brackets_balanced(content[o0:o1]):
                # 区间重叠（异常矩形）或括号不平衡：保守放弃，修正统计
                result.injected -= 1
                result.dropped += 1
                slot = result.stats.get(cat)
                if slot:
                    slot[0] -= 1
                    slot[1] += 1
                continue
            if target.startswith("fn:"):
                if len(inj) > 5 and inj[5] is not None:
                    # 证据驱动剥离：自定义替换文本（如 "$f^{-4/3}$" + "[^2]"）
                    content = content[:o0] + inj[5] + content[o1:]
                else:
                    content = content[:o0] + "[^" + target[3:] + "]" + content[o1:]
                if len(inj) > 4 and not target[3:].isdigit():
                    # 符号脚注：引用点真正写入才把定义段标成同号 label（防孤儿）
                    blocks[inj[4]].note_label = target[3:]
            else:
                content = content[:o0] + "[" + content[o0:o1] + "](" + target + ")" + content[o1:]
            last_start = o0
            if target.startswith("#"):
                result.anchors.add(target[1:])
        b.content = content
    return result
