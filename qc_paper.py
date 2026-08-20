"""单篇产物 QC 自检：转换收尾时对渲染出的 paper.md 做轻量机械检查。

WARN 级（qc_paper_md，只打 WARN 日志走 stderr，不阻断转换流程）：
1. 图/表编号连续性：正文 Fig. N / Figure N / Table N 引用与实际图块/表注
   编号对账，发现断号（如有 Fig.5 无 Fig.4）打 WARN；
2. 结构顺序异常：# References 出现在任何正文节/正文段落之前时打 WARN
   （stage1/2 排序问题的可探测信号）；
3. References 区分段迹象：存在超长单段（软换行堆叠）时打 WARN。

严重级（qc_severe_findings，返回严重问题列表，供 pipeline 完整性闸打回
重解析/降级）：图/表编号断号（同检查 1，断号即内容缺失）+ 页数对照
（页锚标记数明显少于 PDF 实际页数，疑似整页内容丢失）。
"""

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# 实际图块：图片行 alt 短标签，或图注文本行（行首 "Figure N:" / "Figure N."）
_ACTUAL_FIG_RE = re.compile(
    r"^!\[(?:Figure|Fig\.?)\s*(\d+)[\w.\-]*(?:\s*\([a-zA-Z0-9]+\))?\]"
    r"|^(?:Figure|Fig\.?)\s*(\d+)(?:\.\d+)*\s*[:.]\s",
    re.M)
# 实际表注：行首 "Table N:" / "Table N."，或无冒号直排 "Table N Caption"（MinerU pipeline
# 产物形态——caption 首词大写以区别于 "Table 1 shows" 类行首引用句）
_ACTUAL_TBL_RE = re.compile(r"^Table\s*(\d+)(?:\.\d+)*(?:\s*[:.]\s|\s+[A-Z])", re.M)
_REF_HEADING_RE = re.compile(
    r"^#{1,6}\s*(references|references and notes|bibliography|works cited)\s*[:.]?\s*$",
    re.I | re.M)
_HEADING_RE = re.compile(r"^#{1,6}\s+.+", re.M)
_REF_ITEM_RE = re.compile(r"^\[\d+\]")
# 页锚标记：renderer 对 page_anchor 块输出 "<!-- page: N -->"
_PAGE_MARK_RE = re.compile(r"^<!--\s*page:\s*\d+\s*-->\s*$", re.M)

# 页数对照阈值：页标记数 <= int(pdf_pages * 0.6) 判"整页内容丢失"。
# 0.6 只防大开裂——正常解析偶有末页/空白页无锚点，不设太严；
# 取 <= 是为了让真实事故案例命中：zhao2020 重解析 5 页仅存 3 页标记，
# 3 <= int(5*0.6)=3 → 命中
PAGE_COMPLETENESS_RATIO = 0.6


def _strip_frontmatter(text: str) -> str:
    m = re.match(r"^---\n.*?\n---\n", text, re.S)
    return text[m.end():] if m else text


# 图表引用提及的外部归属形态（引用的是**别论文**的图/表，不计入内部引用对账；
# 实证形态：forecast 论文 "following the procedure given in Ref. [55], whose
# Table 2 or Table 3 provides ..."——该文自身零表注，误报"缺 Table 2/3"）：
#   - 前缀 whose（whose 从句修饰 Ref. [N]）
#   - 前缀 "Ref. [55]"/"Refs. [1, 2]"（兼容 [[55](#ref-55)] 链接形态）
#   - 后接 "of/in Ref." / "of/in [55]"（同样兼容链接形态）
#   - 并列延续："whose Table 2 or Table 3" 的 Table 3（前缀 or/and 且前一提及
#     已被判外部归属）
_EXT_PRE_RES = [
    re.compile(r"\bwhose\s+$", re.I),
    re.compile(r"\bRefs?\.?\s*\[+\d+(?:[,\u2013-]\s*\d+)*\]*"
               r"(?:\]\([^)]*\))?\s*[,;:]?\s*$", re.I),
]
_EXT_POST_RE = re.compile(
    r"^\s*(?:of|in)\s+(?:Refs?\.?\b|\[+\d)", re.I)
_CHAIN_RE = re.compile(r"(?:or|and)\s*$", re.I)
_MENTION_RE = re.compile(r"\b(Fig(?:ure)?\.?|Table)\s*(\d+)")


def _internal_fig_table_refs(prose: str) -> tuple[set[int], set[int]]:
    """正文叙述中的内部图/表引用编号集（外部归属形态不计入，宁严勿宽：
    只排除上述明确形态，"Table 2 shows" 类内部引用照常计入）。"""
    figs: set[int] = set()
    tbls: set[int] = set()
    prev_external = False
    prev_end = -10**9
    for m in _MENTION_RE.finditer(prose):
        pre = prose[max(0, m.start() - 60):m.start()]
        post = prose[m.end():m.end() + 40]
        external = (
            any(p.search(pre) for p in _EXT_PRE_RES)
            or _EXT_POST_RE.match(post) is not None
            or (prev_external and m.start() - prev_end < 40
                and _CHAIN_RE.search(pre))
        )
        if external:
            prev_external = True
            prev_end = m.end()
            continue
        prev_external = False
        (figs if m.group(1).lower().startswith("fig") else tbls).add(int(m.group(2)))
    return figs, tbls


def _check_fig_table_continuity(body: str) -> list[str]:
    warns = []
    actual_figs = {int(n) for m in _ACTUAL_FIG_RE.finditer(body)
                   for n in (m.group(1) or m.group(2),) if n}
    actual_tbls = {int(m.group(1)) for m in _ACTUAL_TBL_RE.finditer(body)}
    # 引用集：排除图片行与图注/表注行自身，只统计正文叙述中的引用；
    # 外部归属提及（"whose Table 2" 等）不算内部引用
    prose = "\n".join(
        ln for ln in body.splitlines()
        if not ln.startswith("![")
        and not re.match(r"^(?:Figure|Fig\.?|Table)\s*\d+(?:\.\d+)*\s*[:.]\s", ln))
    ref_figs, ref_tbls = _internal_fig_table_refs(prose)

    def gaps(actual: set[int], referenced: set[int], label: str) -> list[str]:
        out = []
        universe = actual | referenced
        if not universe:
            return out
        for n in range(min(universe), max(universe) + 1):
            if n not in actual:
                hint = "正文有引用" if n in referenced else "正文亦无引用"
                out.append(f"{label} 编号断号: 缺 {label} {n}（{hint}，"
                           f"实际编号 {sorted(actual) or '无'}）")
        return out

    warns += gaps(actual_figs, ref_figs, "Figure")
    warns += gaps(actual_tbls, ref_tbls, "Table")
    return warns


def _check_references_order(body: str) -> list[str]:
    m_ref = _REF_HEADING_RE.search(body)
    if not m_ref:
        return []
    # 第一个"正文内容"：非 References 的标题，或非空正文段落行
    for m in _HEADING_RE.finditer(body):
        if m.start() != m_ref.start():
            first_content_pos = m.start()
            break
    else:
        first_content_pos = None
    para = re.search(r"^(?!\s*$|<!--|!\[|#{1,6}\s)\S", body, re.M)
    if para and (first_content_pos is None or para.start() < first_content_pos):
        first_content_pos = para.start()
    if first_content_pos is not None and m_ref.start() < first_content_pos:
        return ["# References 出现在正文内容之前（疑似 stage1/2 排序异常，"
                "正文节可能被排到 References 之后）"]
    # References 之后仍有正文节标题（如 Experimental）→ 同样说明排序异常
    for m in _HEADING_RE.finditer(body, m_ref.end()):
        title = m.group(0).lstrip("#").strip()
        if not _REF_HEADING_RE.match(m.group(0)):
            return [f"# References 之后仍存在正文节标题（{title!r}），"
                    "疑似 stage1/2 排序异常"]
    return []


def _check_references_segmentation(body: str) -> list[str]:
    m_ref = _REF_HEADING_RE.search(body)
    if not m_ref:
        return []
    # References 区：标题之后到下一个同级/更高级标题或 EOF
    m_next = re.search(r"^#{1,6}\s+\S", body[m_ref.end():], re.M)
    section = body[m_ref.end(): m_ref.end() + m_next.start()] if m_next \
        else body[m_ref.end():]
    warns = []
    for para in re.split(r"\n\s*\n", section):
        para = para.strip()
        if not para:
            continue
        # 未分段迹象：一个段落里合并了多条引用（裸行软换行堆叠，
        # 或单长行内联多条 [n]）。单条长引用（多 sub-entry）属正常，不报。
        n_stacked = sum(1 for ln in para.splitlines() if _REF_ITEM_RE.match(ln))
        n_inline = len(re.findall(r"[.!?][ \t]+\[\d+\][ \t]+[A-Z(]", para))
        if n_stacked + n_inline >= 2:
            warns.append(
                f"References 区存在疑似未分段的长段（{len(para)} 字符，"
                f"合并了约 {n_stacked + n_inline} 条引用）")
    return warns


def qc_paper_md(paper_md_path: Path) -> list[str]:
    """对渲染出的 paper.md 跑 QC 自检，WARN 打 stderr，返回警告列表。"""
    try:
        text = Path(paper_md_path).read_text(encoding="utf-8")
    except OSError as e:
        logger.warning(f"  QC: 无法读取 {paper_md_path}: {e}")
        return []

    body = _strip_frontmatter(text)
    warns: list[str] = []
    warns += _check_fig_table_continuity(body)
    warns += _check_references_order(body)
    warns += _check_references_segmentation(body)

    for w in warns:
        logger.warning(f"  QC WARN: {w}")
    if not warns:
        logger.info("  QC 自检通过，无 WARN")
    return warns


def _check_page_completeness(body: str, pdf_pages: int | None) -> list[str]:
    """页数对照：页锚标记数明显少于 PDF 实际页数 → 疑似整页内容丢失。"""
    # ≤2 页的论文不查：content_processor 会剥掉首页的前导锚点
    # （完整产物的标记数 ≈ 页数-1），1~2 页论文完整也只有 0~1 个标记，
    # 必然误中阈值；且这么短的论文也无"大开裂"可防
    if not pdf_pages or pdf_pages < 3:
        return []
    markers = len(_PAGE_MARK_RE.findall(body))
    if markers <= int(pdf_pages * PAGE_COMPLETENESS_RATIO):
        return [f"页标记 {markers}/{pdf_pages}，疑似整页内容丢失"]
    return []


# ============================================================
# 公式结构完整性（严重级）：引擎偶发把长多分支分段公式截断输出
# （forecast eq57 实测："...( \frac \Omega \end{array}\tag{57}"——\frac 的
# 第二参数落到 \end 上，KaTeX 编译报错红显）。只报近乎确定损坏的形态，
# 拿不准的一律不报（合法形态如 \left\{ ... \right. 右空定界符不算失衡）
# ============================================================
_EQ_BLOCK_RE = re.compile(r"\$\$\n?(.*?)\n?\$\$", re.S)
_EQ_TAG_FIND_RE = re.compile(r"\\tag\*?\{([^{}]*)\}")
# \frac 族的第二参数落在 \end/\begin/\\/块尾 → 必坏（\frac12、\frac\Omega\chi
# 这类单 token 参数是合法 LaTeX，不报）
_EQ_FRAC_BROKEN_RE = re.compile(
    r"\\(?:frac|dfrac|tfrac|cfrac|binom)\s*"
    r"(?:\{(?:[^{}]|\{[^{}]*\})*\}|\\[A-Za-z]+|[^\s\\{])?\s*"  # 参数1（可缺省=块尾截断）
    r"(?:\\(?:end|begin)\b|\\\\|$)")  # 参数2 落在结构标记/块尾 → 必坏


def _check_equation_integrity(body: str) -> list[str]:
    """展示公式块的结构校验（严重级）。返回问题列表。"""
    out = []
    for m in _EQ_BLOCK_RE.finditer(body):
        eq = m.group(1)
        tag_m = _EQ_TAG_FIND_RE.search(eq)
        label = f"\\tag{{{tag_m.group(1)}}}" if tag_m else "（无编号）"
        issues = []
        # 花括号配对（\{ \} 转义不计）
        stripped = eq.replace("\\{", "").replace("\\}", "")
        nl, nr = stripped.count("{"), stripped.count("}")
        if nl != nr:
            issues.append(f"花括号不配对（{{ 有 {nl} 个，}} 有 {nr} 个）")
        # \begin/\end 配对（按环境名计数对账）
        begins = re.findall(r"\\begin\{(\w+\*?)\}", eq)
        ends = re.findall(r"\\end\{(\w+\*?)\}", eq)
        if sorted(begins) != sorted(ends):
            issues.append(f"\\begin/\\end 不配对（begin={begins} end={ends}）")
        # \left/\right 配对（\right. 空定界符也被 \right 计数覆盖，合法）
        nleft = len(re.findall(r"\\left(?![a-zA-Z])", eq))
        nright = len(re.findall(r"\\right(?![a-zA-Z])", eq))
        if nleft != nright:
            issues.append(f"\\left/\\right 不配对（{nleft}/{nright}）")
        # \frac 族参数组缺失/落在结构标记上
        if _EQ_FRAC_BROKEN_RE.search(eq):
            issues.append("\\frac 族命令参数组缺失（疑似公式截断）")
        # \tag 出现在环境内部（应在 \end 之后）
        stack = []
        for tm in re.finditer(r"\\(begin|end)\{\w+\*?\}|\\tag\*?\{", eq):
            tok = tm.group(0)
            if tok.startswith("\\begin"):
                stack.append(tok)
            elif tok.startswith("\\end"):
                if stack:
                    stack.pop()
            elif stack:
                issues.append("\\tag 出现在环境内部")
                break
        for issue in issues:
            out.append(f"公式结构疑似损坏（{label}）: {issue}")
    return out


def _check_references_nonempty(body: str) -> list[str]:
    """参考文献区存在但为空 → 严重级（条目在管线中丢失的可探测信号；
    forecast 实测：引擎以 list/ref_text 形态产出了全部条目，下游不认识
    静默丢弃，paper.md 里 # References 标题下是空的——断号检查抓不住）。
    """
    m_ref = _REF_HEADING_RE.search(body)
    if not m_ref:
        return []
    section = body[m_ref.end():]
    m_next = re.search(r"^#{1,6}\s+\S", section, re.M)
    if m_next:
        section = section[:m_next.start()]
    lines = []
    for ln in section.splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("<!--"):
            continue
        # 剥掉行首锚点标记（<a id="ref-N"></a>[1] ... 的条目行）再判空
        ln = re.sub(r'^(?:<a id="[^"]+"></a>\s*)+', "", ln).strip()
        if ln:
            lines.append(ln)
    if not lines:
        return ["参考文献区为空：存在 References 标题但无任何条目"
                "（疑似条目在管线中丢失）"]
    return []


def qc_severe_findings(paper_md_path: Path, pdf_pages: int | None) -> list[str]:
    """完整性级（严重）检查：图/表编号断号 + 页数明显不足。返回严重问题列表。

    与 qc_paper_md 的 WARN 级检查不同，严重级命中意味着产物内容不完整，
    不应直接交付（pipeline 据此打回重解析/降级，最终仍不解决则 done 打标
    incomplete）。排序/分段两个检查不进严重级——Science/PNAS 的
    Acknowledgments 在 References 后是合法结构，已观察到误报。
    """
    try:
        text = Path(paper_md_path).read_text(encoding="utf-8")
    except OSError as e:
        logger.warning(f"  QC: 无法读取 {paper_md_path}: {e}")
        return []

    body = _strip_frontmatter(text)
    severe: list[str] = []
    # 图/表断号直接复用 WARN 级同一检查：断号即内容缺失，全部算严重级
    severe += _check_fig_table_continuity(body)
    severe += _check_equation_integrity(body)
    severe += _check_references_nonempty(body)
    severe += _check_page_completeness(body, pdf_pages)
    return severe
