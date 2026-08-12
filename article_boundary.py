# 脏 PDF 文章边界切分（"杂志截页"类污染 PDF 的最小锚点实现）。
#
# 场景：PDF 并不完全是目标文献——标题前挂着上一篇文章的结尾与参考文献，
# 本文 References 结束后又跟了下一篇文章的标题与摘要。规则无法可靠判断
# 文章起止，采用锚点主通道（本模块）+ 辅助模型兜底（下一轮，需真实脏样本）。
# spec 见 docs/structure-detection.md 第五节。
#
# 设计原则与封面根修一致：宁可少切，绝不误伤——
#   - 头切：仅在权威标题锚点命中、且锚点前有 substantial 他文时才切；
#   - 尾切：仅在 References 区出现后，保护全部固定段标题
#     （Acknowledgments/Appendix 等在 References 后是合法结构，QC 闸已实证）；
#   - 每次切除输出 INFO 日志，绝不无声丢内容。

import logging
import re

logger = logging.getLogger(__name__)

# 头切触发门槛：标题锚点前的散文块字符总量需超过此值
# （低于它说明前面只是零星噪声块，不值得动刀）
LEAD_TRIM_MIN_PRE_CHARS = 400
# 标题锚点最短长度（太短的标题匹配无判别力）
MIN_ANCHOR_TITLE_LEN = 20
# 尾切保留的固定段标题（References 之后仍合法出现的节）；
# 命中即终止尾切——之后的内容全部保留
_FIXED_SECTIONS = {
    "abstract", "摘要", "acknowledgments", "acknowledgements",
    "acknowledgement", "acknowledgement", "funding", "author contributions",
    "competing interests", "conflict of interest", "conflicts of interest",
    "data availability", "code availability", "materials availability",
    "supplementary materials", "supplementary material", "supplemental information",
    "supporting information", "additional information", "author information",
    "appendix", "appendices", "glossary", "one sentence summary",
}
_REF_HEADINGS = {
    "references", "references and notes", "bibliography", "works cited",
    "literature cited", "参考文献",
}
# 标题规范化：压空白、小写、去首尾标点
def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip().lower().strip(":.：。 ")


def _title_match(block_text: str, norm_title: str) -> bool:
    """块文本是否命中权威标题（规范化后子串匹配，或去标点等价）"""
    bt = _norm(block_text)
    if not bt:
        return False
    if norm_title in bt:
        return True
    # 等价容错：标点替换为空格并压空白后相等（OCR 常把标题里的连字符/引号
    # 识别变形，如 "sodium-ion" → "sodium ion"）
    strip = lambda s: re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", s)).strip()  # noqa: E731
    return strip(bt) == strip(norm_title)


def trim_pre_title_content(blocks: list, title: str) -> list:
    """头切：切掉目标标题锚点之前的他文内容（上一篇文章的残留）。

    在 IR 块列表上操作；page_anchor 全部保留（页计数不失真），只切
    锚点前的内容块。不满足触发条件时原样返回（不做拷贝）。

    Args:
        blocks: ProcessedBlock 列表（_build_ir 之后、标题分类之前）
        title: 权威标题（Zotero/规则提取）；为空或过短时不执行
    """
    norm_title = _norm(title)
    if len(norm_title) < MIN_ANCHOR_TITLE_LEN:
        return blocks

    anchor = None
    pre_prose = 0
    for i, b in enumerate(blocks):
        if b.kind == "page_anchor":
            continue
        if b.kind in ("heading", "paragraph") and _title_match(b.content, norm_title):
            anchor = i
            break
        if b.kind == "paragraph" and b.content:
            pre_prose += len(b.content)

    if anchor is None:
        return blocks
    # 锚点就是第一个内容块（或前面只有零星噪声）→ 正常论文，不动刀
    content_before = [b for b in blocks[:anchor] if b.kind != "page_anchor"]
    if not content_before or pre_prose < LEAD_TRIM_MIN_PRE_CHARS:
        return blocks

    kept = [b for i, b in enumerate(blocks)
            if i >= anchor or b.kind == "page_anchor"]
    logger.info(
        f"  文章边界: 标题锚点前切除 {len(content_before)} 块他文内容"
        f"（约 {pre_prose} 字符，疑似上一篇文章残留）")
    return kept


def trim_post_references_tail(blocks: list) -> list:
    """尾切：References 区之后出现的非固定段标题及其后续内容视为下一篇文章，切除。

    严格约束（防误伤，Science/PNAS 的 Acknowledgments 在 References 后合法）：
    - 必须先出现 References 区标题；
    - 遇到任一固定段标题（Acknowledgments/Appendix/Funding...）立即终止尾切，
      其后内容全部保留；
    - 参考文献条目（reference 块）与页锚不受影响。
    """
    ref_start = None
    for i, b in enumerate(blocks):
        if b.kind == "heading" and _norm(b.content) in _REF_HEADINGS:
            ref_start = i
            break
    if ref_start is None:
        return blocks

    cut = None
    for i in range(ref_start + 1, len(blocks)):
        b = blocks[i]
        if b.kind == "heading":
            h = _norm(b.content)
            if h in _FIXED_SECTIONS:
                return blocks  # 合法后置固定段 → 放弃尾切
            # References 后的非固定段标题 = 下一篇文章的开始
            cut = i
            break
    if cut is None:
        return blocks

    removed = [b for b in blocks[cut:] if b.kind != "page_anchor"]
    if not removed:
        return blocks
    kept = blocks[:cut] + [b for b in blocks[cut:] if b.kind == "page_anchor"]
    logger.info(
        f"  文章边界: References 后切除 {len(removed)} 块"
        f"（始于标题 {blocks[cut].content[:40]!r}，疑似下一篇文章）")
    return kept


def apply_article_boundary(blocks: list, title: str) -> list:
    """边界切分入口：头切 + 尾切（均为锚点强信号触发，弱信号不动刀）"""
    blocks = trim_pre_title_content(blocks, title)
    blocks = trim_post_references_tail(blocks)
    return blocks
