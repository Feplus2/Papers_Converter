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
    for i, ch in enumerate(text):
        # NFKC 不抹平弯引号/连接号，显式归一（引擎与 PDF 字符流常因此错位）
        ch = ch.translate(_CHAR_FOLD)
        for c in unicodedata.normalize("NFKC", ch).casefold():
            if c.isspace():
                pending = True
                continue
            if pending and not prev_ws:
                sk.append(" ")
                s2o.append(i)
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


@dataclass
class LinkResult:
    anchors: set = field(default_factory=set)  # 被链接指向的锚点 id（renderer 据此发射）
    injected: int = 0
    dropped: int = 0
    stats: dict = field(default_factory=dict)  # 类别 -> [注入数, 放弃数]


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
                    dest_page = lk.get("page", -1)
                    to = lk.get("to")
                    if to is not None:
                        dest_x, dest_y = to.x, to.y
                    if dest_page < 0 and kind == fitz.LINK_NAMED and dest_name:
                        # 链接未自带目标 → resolve_names() 解析；失败则放弃
                        nm = names.get(dest_name)
                        if not nm or nm.get("page", -1) < 0:
                            continue
                        dest_page = nm["page"]
                        to = nm.get("to")
                        if to is not None:
                            dest_x, dest_y = to.x, to.y
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
# equation 整个跳过；table 的 HTML 表体不注入）
_INJECTABLE_KINDS = {"paragraph", "reference", "image", "table_image"}
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

    def map_span(self, bm: _BlockMap, link: "_Link") -> tuple[int, int] | None:
        """链接源字符区间 → 块原文偏移 [o0, o1)。不在同一等值段内 → None。

        置信度：等值段须比区间长出上下文（≥4 字符），或触及块首/尾
        （孤立的 3 字符巧合匹配不足为凭）。
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
                if context < 4 and not touches_edge:
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

    # 每个块待注入区间（块下标 → [(o0, o1, target)]）
    injections: dict[int, list[tuple[int, int, str]]] = {}
    math_spans: dict[int, list[tuple[int, int]]] = {}
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
        if dest.startswith(("frontmatter", "footnote")) or "-footnote" in dest:
            return None, "other"  # 无对应锚点契约
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
            return None, "other"  # 未识别的 named dest 类型，不猜

        # LINK_GOTO（无 nameddest）：只能靠源文字形态分类 + 位置映射校验
        if m_cite and text.startswith("["):
            num = m_cite.group(1)
            idx = _target_block(lk)
            if idx is not None and blocks[idx].kind == "reference" \
                    and block_anchor_id(blocks[idx]) == f"ref-{num}":
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
                continue
            o0, o1 = offsets
            # 区间守卫：块内子串骨架须与链接源文字一致（防错位注入）
            sub_sk = _skeletonize(b.content[o0:o1])[0]
            want_sk = _skeletonize(
                pages[lk.page].raw[lk.c0:lk.c1])[0]
            if sub_sk != want_sk:
                continue
            # 数学段内不注入
            spans = math_spans.get(idx)
            if spans is None:
                spans = [(m.start(), m.end())
                         for m in _MATH_SPAN_RE.finditer(b.content)]
                math_spans[idx] = spans
            if any(o0 < e and o1 > s for s, e in spans):
                break  # 命中数学段：位置已明但规则禁止注入，按放弃计
            placed = (idx, o0, o1)
            break
        # 目标映射 + 校验（URI 白名单兜底要看落在哪种块里）
        target, cat = _resolve(lk, blocks[placed[0]].kind if placed else None)
        if target is not None and placed is not None:
            injections.setdefault(placed[0], []).append(
                (placed[1], placed[2], target, cat))
            _stat(cat, True)
        else:
            _stat(cat, False)

    # 应用注入：同块多区间按偏移倒序逐个拼接，互不干扰。
    # 先就地合并同目标的相邻区间（跨行链接常被拆成多个矩形，如
    # "arXiv:xxx [astro-" + "ph.CO]"，合并后显示文字方括号恢复平衡）；
    # 合并后仍不平衡的放弃（不平衡括号会破坏 Markdown 链接语法与 A/B 剥离）
    for idx, spans in injections.items():
        b = blocks[idx]
        merged: list[list] = []
        for o0, o1, target, cat in sorted(spans):
            if merged and merged[-1][2] == target and merged[-1][1] == o0:
                merged[-1][1] = o1  # 紧邻同目标：延伸前一区间
            else:
                merged.append([o0, o1, target, cat])
        content = b.content
        last_start = len(content) + 1
        for o0, o1, target, cat in reversed(merged):
            if o1 > last_start or not _brackets_balanced(content[o0:o1]):
                # 区间重叠（异常矩形）或括号不平衡：保守放弃，修正统计
                result.injected -= 1
                result.dropped += 1
                slot = result.stats.get(cat)
                if slot:
                    slot[0] -= 1
                    slot[1] += 1
                continue
            content = content[:o0] + "[" + content[o0:o1] + "](" + target + ")" + content[o1:]
            last_start = o0
            if target.startswith("#"):
                result.anchors.add(target[1:])
        b.content = content
    return result
