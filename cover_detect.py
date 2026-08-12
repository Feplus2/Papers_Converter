# 封面页判定（统一实现，供 content_processor / metadata 共用）
#
# 事故背景（2026-08-12，zhao2020rational）：旧实现把页全文拼成一个大字符串，
# 数 7 个关键词命中数 >=2 即整页丢弃，且检查 page 0/1 两页。Science 期刊每页
# 页眉标配 "Downloaded from ..."，正文引文又常含 "university of technology"，
# 两个高频正文词凑一对即触发——zhao2020 的正文第二页被整页静默切除（文字+图），
# 换任何 OCR 引擎都在同一页复现（丢页发生在引擎产物之后的规则层，是确定性行为）。
# 详见 docs/structure-detection.md。
#
# 新判据（层层收紧，宁可漏切噪声封面也绝不误杀正文）：
#   1. 只判 page 0——封面只可能在首页，page 1 及以后永不判封面；
#   2. 命中标记 >= COVER_MARKER_THRESHOLD（真封面样本 26NNZJHX 是 7/7，余量充足）；
#   3. 正文信号一票否决：image/equation/table 块、长散文块（摘要/正文段落形态）、
#      散文总量超阈值、section 编号标题——任一命中即非封面；
#   4. 元数据锚定否决：权威标题/DOI 与页内文本重合且标记偏弱（<5）——真仓库
#      封面模板标记通常全套（>=5），标记偏弱却含权威标题更像真首页。
#
# 实测分界（zotero-brain 126 篇语料）：真封面 prose<=1233 字符、无富内容块、
# 长块(>300) 为 0；真首页 prose>=2559、有 image/equation 或多个长摘要段。

import logging
import re

logger = logging.getLogger(__name__)

# 仓库封面模板关键词（TU/e Pure 类机构仓库的引用声明页）
COVER_PAGE_MARKERS = [
    "university of technology",
    "citation (apa)",
    "document version",
    "important note",
    "takedown policy",
    "downloaded from",
    "for technical reasons",
]

# 判封面所需的最少标记命中数（旧阈值 2 是事故根因；真封面样本 7/7）
COVER_MARKER_THRESHOLD = 3
# 散文总量否决阈值：封面模板文本短小（实测 1233），真首页 >=2559
COVER_PROSE_VETO_CHARS = 1500
# 单块长散文否决阈值：摘要/正文段落形态（封面模板行最长实测 263）
COVER_LONG_BLOCK_VETO_CHARS = 300
# 锚定否决生效的标记数上限：标记 >=5 视为模板全套（真封面），不再锚定否决
_ANCHOR_VETO_MAX_MARKERS = 5
# 富内容块类型：任一出现即非封面
_RICH_BLOCK_TYPES = {"image", "equation", "table"}
# 噪声块类型（不计入判定文本）
_NOISE_BLOCK_TYPES = {"header", "footer", "page_number", "aside_text"}
# section 编号标题（"1. Introduction" / "1 Introduction" 形态）
_SECTION_HEADING_RE = re.compile(r"^\s*\d+(?:\.\d+)?\.?\s+[A-Z]\w")


def _page_blocks(content_list: list[dict], page_idx: int = 0) -> list[dict]:
    return [b for b in content_list if b.get("page_idx", 0) == page_idx]


def _title_matches(text_lower: str, title: str) -> bool:
    """标题是否出现在页文本中（压空白后子串匹配，容忍换行切分）"""
    if not title:
        return False
    norm_title = re.sub(r"\s+", " ", title.strip().lower())
    if len(norm_title) < 15:  # 太短的标题匹配无判别力
        return False
    return norm_title in text_lower


def legacy_detect_cover_pages(content_list: list[dict]) -> set[int]:
    """旧实现原样保留（仅供 AB 对照，管线不再调用）。

    拼页全文数关键词 >=2 即判封面，检查 page 0/1。已知缺陷见模块 docstring。
    """
    cover_pages = set()
    for page_idx in range(2):
        page_text = " ".join(
            block.get("text", "").lower()
            for block in content_list
            if block.get("page_idx", 0) == page_idx
        )
        markers_found = sum(1 for m in COVER_PAGE_MARKERS if m in page_text)
        if markers_found >= 2:
            cover_pages.add(page_idx)
    return cover_pages


def _judge_page0(page: list[dict], title: str = "", doi: str = "") -> tuple[bool, str]:
    """判定 page 0 是否为仓库封面页。返回 (是否封面, 判定依据描述)。"""
    texts = []
    for b in page:
        if b.get("type") in _NOISE_BLOCK_TYPES:
            continue
        if b.get("type") in _RICH_BLOCK_TYPES:
            return False, f"含富内容块 {b.get('type')}，是正文页"
        t = (b.get("text") or "").strip()
        if t:
            texts.append(t)

    page_lower = " ".join(texts).lower()
    hits = [m for m in COVER_PAGE_MARKERS if m in page_lower]

    if len(hits) < COVER_MARKER_THRESHOLD:
        return False, f"标记命中 {len(hits)}/{COVER_MARKER_THRESHOLD} 不足（{hits}）"

    # 正文信号一票否决（标记够数才走到这里）
    prose_chars = sum(len(t) for t in texts)
    if prose_chars > COVER_PROSE_VETO_CHARS:
        return False, f"标记 {len(hits)} 个但散文 {prose_chars} 字符超阈值 {COVER_PROSE_VETO_CHARS}"
    long_blocks = [len(t) for t in texts if len(t) > COVER_LONG_BLOCK_VETO_CHARS]
    if long_blocks:
        return False, f"存在长散文块 {long_blocks}（摘要/正文形态）"
    for t in texts:
        if _SECTION_HEADING_RE.match(t) and len(t) < 80:
            return False, f"存在编号章节标题 {t[:40]!r}"

    # 元数据锚定否决：标记偏弱（<5）且权威标题/DOI 出现在页内 → 保留页面
    if len(hits) < _ANCHOR_VETO_MAX_MARKERS:
        if _title_matches(page_lower, title):
            return False, f"标记偏弱（{len(hits)}）且权威标题出现在页内"
        if doi and doi.lower() in page_lower:
            return False, f"标记偏弱（{len(hits)}）且权威 DOI 出现在页内"

    return True, f"仓库封面模板标记命中 {len(hits)}/7（{hits}），无正文信号"


def detect_cover_pages(content_list: list[dict], title: str = "",
                       doi: str = "",
                       use_llm: bool | None = None) -> set[int]:
    """检测仓库封面页（如 TU/e Pure 的引用声明封面）。

    只可能返回 {0} 或空集——封面只可能在首页。判定依据写 INFO 日志，
    不再无声丢页。

    Args:
        content_list: 引擎解析的 content_list.json 内容
        title: 权威标题（Zotero/规则提取），用于锚定否决，可空
        doi: 权威 DOI，用于锚定否决，可空
        use_llm: 是否走辅助模型仲裁。None=取 config.STRUCTURE_LLM（默认关）；
            AB 对照/单测显式传 False 隔离
    """
    page0 = _page_blocks(content_list, 0)
    if not page0:
        return set()
    is_cover, reason = _judge_page0(page0, title=title, doi=doi)
    rule_cover = {0} if is_cover else set()
    if is_cover:
        logger.info(f"  封面判定: page 0 判为仓库封面页（{reason}），将跳过")
    else:
        logger.info(f"  封面判定: 无封面页（page 0: {reason}）")

    # 辅助模型仲裁（默认关；冲突裁决保守方向优先，见 structure_llm）
    if use_llm is None:
        import config
        use_llm = config.STRUCTURE_LLM
    if use_llm:
        from structure_llm import llm_cover_review
        return llm_cover_review(content_list, rule_cover)
    return rule_cover
