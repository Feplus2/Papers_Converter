"""Stage 2: 正文清洗与结构重建。

将 MinerU content_list 处理为结构化的中间表示（IR），
供 renderer 渲染为 Pandoc Markdown。
"""

import json
import logging
import re
from html import unescape
from pathlib import Path

import config
from article_boundary import apply_article_boundary
from cover_detect import detect_cover_pages
from link_extractor import _REF_NUM_RE  # 条目编号解析与 #ref-N 锚点同源

logger = logging.getLogger(__name__)


# PaddleOCR-VL 下标小数/分数断裂修复：
# 上游把十进制点/分数线切断在数学区外（"$Li_{0$.75}" / "$Li_{1$/3}$"），
# 产生 { 未闭合的 span（pandoc/texmath "unexpected eof"，KaTeX 必挂；全量实测
# 1001 处，是契约"公式 KaTeX 渲染无报错"唯一大面积不达项）。
# "{0$.75}" → "{0.75}"，"{1$/3}" → "{1/3}"；左侧兼容变量/符号（"{x$.5}"、
# "{3+$/4+}" 价态、"{0$.5-$x}" 区间），分隔符兼容欧式逗号小数（"{0$,78}"）。
_BROKEN_SCRIPT_PAREN_RE = re.compile(r"\{([\w()+-]*)\$\)\$\}")  # "{(1-x$)$}" → "{(1-x)}"
_BROKEN_SCRIPT_PAREN2_RE = re.compile(r"\{([\w()+-]*)\$(\))")  # "{(O-Na-O$)}" → "{(O-Na-O)}"
_BROKEN_SCRIPT_PRIME_RE = re.compile(r"\{(\w*)\$(')")  # "$O_{2$'}$" → "$O_{2'}$"
_BROKEN_SCRIPT_FRAC_RE = re.compile(r"\{([\w()+-]*)\$([.,/\\-])([\w$().+-]*)\}")
_BROKEN_SCRIPT_CMD_RE = re.compile(r"\{([\w()+-]*)\$(\\[a-zA-Z]+[\w().+-]*)\}")  # "{3+$\delta}" → "{3+\delta}"
_BROKEN_SCRIPT_CLOSE_RE = re.compile(r"\{([\w()+-]*)\$(\})")  # "$g^{-1$}$" → "$g^{-1}$"
_BROKEN_SCRIPT_SLASH_RE = re.compile(r"_\{(\w+)\}/(\w+)\}")  # "$Na_{2}/3}$" → "$Na_{2/3}$"
_BROKEN_RBRACE_SUP_RE = re.compile(r"\$(\w+)\}\^")  # "$Na}^+$" → "$Na^+$"（} 越位）
# 伪公式包裹污染 \mathrm（stage1 把 \mathrm 内容再包 $...$）："\mathrm{$Na}" → "\mathrm{Na}"
# （$ 漏进数学区会让 pandoc 重新切 span 并吃掉 \mathrm{ 导致 eof）
_MATHRM_WRAP_UNWRAP_RE = re.compile(r"\\mathrm\{\$([A-Za-z0-9]+)\}")

# MinerU VLM 多行公式编号拆行伪影：编号 "(50)" 沿 array 行竖排拆开时，VLM
# 先给首行残缺编号生成 \tag{5}、末尾又补完整 \tag{50}，双 \tag 被 KaTeX 拒收
# （"Multiple \tag"）。实例：cosmic strings 论文 Eq.(50) / Eq.(A2)。
_EQUATION_TAG_RE = re.compile(r"\\tag\*?\s*\{[^{}]*\}")


def _dedup_equation_tags(text: str) -> str:
    """同一条公式出现多个 \\tag 时只保留一个，其余删除。

    保留内容最长者（最完整的编号），并列取最末——伪影形态是残缺编号在前、
    完整编号在后（"\\tag{5}\\tag{50}" / "\\tag{A}\\tag{A2}"），取最末与取最长
    结论一致；用最长是防 "\\tag{50}\\tag{5}" 这类倒序残缺。
    """
    ms = list(_EQUATION_TAG_RE.finditer(text))
    if len(ms) <= 1:
        return text
    keep = max(range(len(ms)), key=lambda i: (len(ms[i].group(0)), i))
    out, prev = [], 0
    for i, m in enumerate(ms):
        if i == keep:
            out.append(text[prev:m.end()])
        else:
            out.append(text[prev:m.start()])
        prev = m.end()
    out.append(text[prev:])
    return "".join(out)


def _frac_merge(m: re.Match) -> str:
    # 右侧可能还嵌着第二个 $（变量被单独切出，如 "{0$.5-$x}"）——一并去掉
    return "{" + m.group(1) + m.group(2) + m.group(3).replace("$", "") + "}"


def _repair_script_frac(text: str) -> str:
    text = _BROKEN_SCRIPT_PAREN_RE.sub(r"{\1)}", text)
    text = _BROKEN_SCRIPT_PAREN2_RE.sub(r"{\1\2", text)
    text = _BROKEN_SCRIPT_PRIME_RE.sub(r"{\1\2", text)
    text = _BROKEN_SCRIPT_FRAC_RE.sub(_frac_merge, text)
    text = _BROKEN_SCRIPT_CMD_RE.sub(r"{\1\2}", text)
    text = _BROKEN_SCRIPT_CLOSE_RE.sub(r"{\1\2", text)
    text = _BROKEN_SCRIPT_SLASH_RE.sub(r"_{\1/\2}", text)
    text = _BROKEN_RBRACE_SUP_RE.sub(r"$\1^", text)
    text = _MATHRM_WRAP_UNWRAP_RE.sub(r"\\mathrm{\1}", text)
    return _balance_math_braces(text)


# 数学段花括号失衡修补（wang2024 实测：引擎把表格化学式输出为整体多一个
# 开括号——"{\mathrm{Na}_{0.7}...{\mathrm{O}}_{2}" 末尾少一个 }，KaTeX
# 拒渲染导致 LaTeX 源码外露）。只修"多开"方向且在段尾补 } 即平的形态；
# 多闭/补不平一律不动
_MATH_SEG_RE = re.compile(r"\$\$.*?\$\$|\$[^$\n]+?\$", re.S)


def _balance_math_braces(text: str) -> str:
    def _fix(m: re.Match) -> str:
        seg = m.group(0)
        if seg.startswith("$$"):
            inner, pre, post = seg[2:-2], "$$", "$$"
        else:
            inner, pre, post = seg[1:-1], "$", "$"
        bare = inner.replace("\\{", "").replace("\\}", "")
        deficit = bare.count("{") - bare.count("}")
        if 0 < deficit <= 3:
            fixed = inner + "}" * deficit
            fb = fixed.replace("\\{", "").replace("\\}", "")
            if fb.count("{") == fb.count("}"):
                return pre + fixed + post
        return seg
    return _MATH_SEG_RE.sub(_fix, text)


# 引文上标化（"$^{[1-7]}" / "$^{[12]}" / "$^{[13b]}$"）回改契约形式（"[1-7]" 等）；
# 兼容收尾空格（"$^{[42]} $"）与 GB/T 文献类型标记（"$^{[J]}$" 单大写字母）；
# 小写字母 [a]/[b] 是作者单位脚注标记，不归一
_CITATION_SUP_RE = re.compile(r"\$\^\{\[((?:\d[\w,;\s–—-]*|[A-Z]))\]\}\s*\$")
# 无收尾 $ 的引文上标（"$...}$^{[52]}" —— $ 是前一个数学区的收尾，^{[n]} 是裸上标）：
# 仅归一 ^{[n]} 部分，前导 $ 必须保留（否则前区失去闭合）
_CITATION_SUP_DANGLING_RE = re.compile(r"(?<=\$)\^\{\[((?:\d[\w,;\s–—-]*|[A-Z]))\]\}(?!\s*\$)")


def _normalize_citation_sup(text: str) -> str:
    text = _CITATION_SUP_RE.sub(r"[\1]", text)
    return _CITATION_SUP_DANGLING_RE.sub(r"[\1]", text)


# 引文簇被引擎误判为行内公式（$[2, 3]$ / $[4–8]$ / $[15, 19–22]$）：纯引文形态的
# $...$ 拆掉 $ 壳还原为文本。数学段内不注入链接是硬规则，不拆壳这些引文永远
# 无法成链（宇宙弦 Introduction 密集引用区实测整片 $[N]$ 形态）。
# 已知残余风险：区间记法 $[0,1]$ 与引文同形会被一并拆壳——可见文本不变，
# 仅失去数学样式，且此类区间在正文中通常不带链接注释，影响可控
_CITATION_MATH_RE = re.compile(
    r"\$\s*(\[\d+(?:\s*[,\u2013\u2014;-]\s*\d+)*\])\s*\$")


def _normalize_inline(text: str) -> str:
    r"""归一化 MinerU 文本中的内联 HTML 与特殊空白。

    现行 VLM 输出在正文里夹带 <sup>/<sub> HTML 标签与   不换行空格
    （旧产物是 $^{}$ LaTeX 形式），统一转为 LaTeX 上/下标，避免标签漏进 paper.md。
    附带：断裂上下标修复（_repair_script_frac）与引文格式归一（$^{[n]}$ → [n]）。
    """
    text = _repair_script_frac(text)
    if "<" in text:
        text = re.sub(r"<sup>\s*</sup>", "", text)
        text = re.sub(r"<sub>\s*</sub>", "", text)
        # 纯脚注符号（\* * † ‡ § ¶ # ☒ ✉）不包 LaTeX（$^{\*}$ 等非法 KaTeX）
        text = re.sub(r"<sup>([\\*†‡§¶#☒✉\s]+)</sup>", r"\1", text)
        text = re.sub(r"<sup>(.*?)</sup>", r"$^{\1}$", text, flags=re.S)
        text = re.sub(r"<sub>(.*?)</sub>", r"$_{\1}$", text, flags=re.S)
        # 样式标签（<i>/<em>/<b>/<u>/<span>）只去标签留内容（Zotero 标题常带 <i>via</i>）
        text = re.sub(r"</?(?:i|em|b|u|span)(?:\s[^>]*)?>", "", text)
    # MinerU 引文上标化（"$^{[1-7]}" / "$^{[12]}"）回改契约形式（"[1-7]" / "[12]"）；
    # 含上一条 <sup>[n]</sup> → "$^{[n]}$" 的转换结果，两条路径殊途同归；
    # 兼容子标签引文（"$^{[13b]}$"）、收尾空格（"$^{[42]} $"）与 GB/T 标记（"$^{[J]}$"）
    text = _normalize_citation_sup(text)
    # 引文簇误判为行内公式（"$[2, 3]$"）拆 $ 壳（见 _CITATION_MATH_RE 注释）
    text = _CITATION_MATH_RE.sub(r"\1", text)
    if "\xa0" in text:
        text = text.replace("\xa0", " ")
    return text

# 噪声块类型（直接丢弃）
_NOISE_TYPES = {"header", "footer", "page_number", "aside_text"}

# 封面页判定已移至 cover_detect.py 统一实现（2026-08-12 封面误判事故根修：
# 旧实现拼页全文数关键词 >=2 即整页丢弃，zhao2020 正文第二页被静默切除，
# 详见 docs/structure-detection.md）

# 固定段标题（不自动编号）
_FIXED_SECTIONS = {
    "abstract", "摘要", "acknowledgments", "acknowledgements", "references",
    "references and notes", "supplementary materials", "supporting information",
    "author contributions", "competing interests", "data availability",
    "methods", "experimental", "experimental section",
    "one sentence summary", "highlights", "keywords", "graphical abstract",
    "authorship", "author information", "additional information",
    "code availability", "materials availability", "supplemental information",
    "conflict of interest", "conflicts of interest", "acronyms",
}

# 正文锚点词：Nature 式无编号论文里的顶级章节词（Introduction/Methods/Results...）。
# 它们本身是顶级标题；锚点之后出现的其他无编号标题视为其子节（##）。
_BODY_ANCHOR_WORDS = {
    "introduction", "background", "methods", "methodology",
    "experimental", "experimental section", "experimental procedures",
    "materials and methods", "results", "results and discussion",
    "discussion", "conclusion", "conclusions", "summary",
    "outlook", "perspective",
}

# 出版信息噪声模式（Nature/Springer/IOP/Elsevier 等出版社的封面元数据残留）
_PUB_NOISE_RES = [
    re.compile(r"^Article\s+https?://doi\.org/", re.I),
    re.compile(r"^https?://doi\.org/\S+\s+(Received|Accepted|Published|RECEIVED|\d{4})", re.I),
    re.compile(r"^To cite this article", re.I),
    re.compile(r"^Cite as\b", re.I),
    re.compile(r"^You may also like\b", re.I),
    re.compile(r"^www\.\S+\s*$", re.I),
    re.compile(r"^\S+\.(org|de|li)/\S*\s*$", re.I),
    re.compile(r"^©", re.I),
    re.compile(r"Creative Commons", re.I),
    re.compile(r"open[- ]access article distributed", re.I),
    re.compile(r"^This is an open access", re.I),
    re.compile(r"^Copyright:", re.I),
    re.compile(r"^Check for updates\s*$", re.I),
    re.compile(r"^OPEN ACCESS\b", re.I),
    re.compile(r"^https?://doi\.org/\S+\s*$", re.I),
    re.compile(r"^10\.\d{4,}/\S+\s*$", re.I),   # 裸 DOI行（无 https 前缀）
    re.compile(r"\b(View the article online|Terms of service|Use of this article|Reprints and permissions)\b", re.I),
    re.compile(r"^(Permissions|Download PDF|Cite this|Metrics|Share|Read Online)\s*$", re.I),
    # 出版日期行（关键词+日期/月份/online，避免误伤 "Published studies..." 正文）
    re.compile(r"^(Received|Accepted|Revised|Published|Final version)\s*[:,]?\s*(\d|January|February|March|April|May|June|July|August|September|October|November|December|online|in revised|for publication)", re.I),
    re.compile(r"^(RECEIVED|ACCEPTED|PUBLISHED)\b", re.I),
    re.compile(r"^Received in revised", re.I),
    re.compile(r"^(Published|Downloaded) on\b", re.I),   # RSC 页脚残留（无前缀可剥时整块丢弃）
    # 独立的续表标记行（"Table 1 (Contd.)"），表已在别处，标记本身是噪声
    re.compile(r"^Table\s+\d+[^()]*\((contd\.?|continued|cont\.?|cont'd)\)\s*$", re.I),
]

# 文章类型标签（整块仅是一个标签词）
_ARTICLE_LABELS = {
    "article", "review", "communication", "research article", "topical review",
    "minireview", "editorial", "letter", "perspective", "comment", "erratum",
    "tutorial review",
}

# 噪声标题（被误判为 heading 的出版元数据）
_NOISE_HEADINGS = {"you may also like", "journal of physics", "contents", "chemical reviews"}


def _clean_paragraph(text: str):
    """清洗出版信息噪声。返回 None 表示纯噪声应丢弃，否则返回清洗后的文本。

    处理两类：
    1. 纯噪声块（文章类型标签/独立 DOI+日期/版权/网址/推荐列表）→ 丢弃
    2. 粘连块（"...Check for updates <正文>"）→ 剥离前缀保留正文
    """
    t = text.strip()
    if not t:
        return None

    # 剥离 RSC 页脚前缀（"Published on 17 May 2010. Downloaded on 29/07/2013 19:30:29. "），
    # 保留粘连的正文（如表注 "Table 1 Selection of ..."）；剥完为空则下面按纯噪声丢弃
    t = re.sub(r"^(?:(?:Published|Downloaded) on\s+[^.]*\.\s*)+", "", t)

    # 剥离 RSC 导航/引用碎片（"View Article Online View Journal | View Issue
    # CrossMark ← click for updates"、"Cite this: ..."、裸 "DOI: x" 前缀）
    t = re.sub(r"\bView (?:Article Online|Journal|Issue)\b", "", t)
    t = re.sub(r"\bCrossMark\s*←?\s*click for updates\b", "", t, flags=re.I)
    t = re.sub(r"^Cite this:.*$", "", t, flags=re.M)
    t = re.sub(r"^DOI:\s*\S+\s+", "", t)
    t = re.sub(r"\s{2,}", " ", t).strip(" |")
    t = t.strip()
    if not t:
        return None

    # 纯噪声模式
    for pat in _PUB_NOISE_RES:
        if pat.search(t):
            # 但若是 "Published online: ... Check for updates <正文>"，剥离前缀保留正文
            m = re.search(r"\bCheck for updates\s+", t)
            if m and m.start() < 250 and len(t[m.end():].strip()) > 150:
                return t[m.end():].strip()
            return None

    # 文章类型标签（整块就是一个标签词，如 "Article" / "TOPICAL REVIEW • OPEN ACCESS"）
    letters_only = re.sub(r"[^a-zA-Z ]", " ", t)
    if len(t) < 60 and re.sub(r"\s+", " ", letters_only).strip().lower() in _ARTICLE_LABELS:
        return None

    # 栏目名/分类标签（如 "LITHIUM BATTERIES"）：短小且匹配噪声标题模式
    if len(t) < 60 and any(p.search(t) for p in _NOISE_HEADING_RES):
        return None

    # 剥离 "Article https://doi.org/... " 前缀
    t = re.sub(r"^Article\s+https?://doi\.org/\S+\s+", "", t)
    # 剥离 Nature "...Check for updates " 前缀
    m = re.search(r"\bCheck for updates\s+", t)
    if m and m.start() < 250:
        t = t[m.end():].strip()
    return t or None


class ProcessedBlock:
    """处理后的内容块（IR）"""

    def __init__(self, kind: str, content: str = "", **kwargs):
        self.kind = kind  # "heading", "paragraph", "image", "equation", "reference", "page_anchor", "table"
        self.content = content
        self.level = kwargs.get("level", 1)  # heading level
        self.caption = kwargs.get("caption", "")
        self.img_src = kwargs.get("img_src", "")  # 原始图片路径
        self.img_new_name = kwargs.get("img_new_name", "")  # 重命名后
        self.page_idx = kwargs.get("page_idx", 0)
        self.bbox = kwargs.get("bbox")  # 页面归一化坐标（图组并集重裁用，见 figure_merger）
        # 源 PDF 页码（0 基；PDF 原生链接注入用，见 link_extractor）。
        # 与 page_idx 分离：page_idx 只标图片/表格块且有合并语义（_merge_paragraph_fragments
        # 依赖现状），src_page 是纯溯源属性，不参与任何既有逻辑
        self.src_page = kwargs.get("src_page")

    def __repr__(self):
        return f"<{self.kind}: {self.content[:50]}...>"


def process_content(content_list: list[dict], images_dir: str = "",
                    use_llm: bool = False, title: str = "") -> list[ProcessedBlock]:
    """
    处理 content_list，返回结构化的 ProcessedBlock 列表。

    Args:
        content_list: MinerU content_list.json 内容
        images_dir: 原始图片目录路径
        use_llm: 是否用 LLM 辅助标题结构分类（区分章节/图注/噪声）
        title: 论文标题（供标题分类参考）

    Returns:
        ProcessedBlock 列表
    """
    # Step 1: 检测并跳过封面页（只可能判 page 0，判定依据带日志，见 cover_detect）
    cover_pages = detect_cover_pages(content_list, title=title)

    # Step 2: 过滤噪声块（page_footnote 不再丢弃——作者单位/正文脚注是正文
    # 一部分，丢失违反文本零丢失红线；在 _build_ir 里落成独立 footnote 块）。
    # 被丢的页眉/页脚块保留在 dropped_noise（页眉池）：引擎偶把章节标题误判为
    # header（martins2000 的 IV. 标题实测），供断档捞回
    filtered = []
    dropped_noise = []
    for block in content_list:
        page_idx = block.get("page_idx", 0)
        if page_idx in cover_pages:
            continue
        if block["type"] in _NOISE_TYPES:
            dropped_noise.append(block)
            continue
        filtered.append(block)

    # Step 2.5: 跨页表格合并（借鉴 MinerU-Popo table_merge_filter 的规则集：
    # 候选配对/caption 一致性/列数兼容/表头去重，纯规则、保守拒绝）
    filtered = _merge_cross_page_tables(filtered)

    # Step 3: 构建 IR
    blocks = _build_ir(filtered, images_dir)

    # Step 3.5: 脏 PDF 文章边界切分（杂志截页类：标题锚点头切 + References 后尾切，
    # 锚点强信号触发，弱信号不动刀；见 article_boundary 与 docs/structure-detection.md）
    if config.ARTICLE_BOUNDARY:
        blocks = apply_article_boundary(blocks, title)

    # Step 4: 标题结构分类（区分章节/图注/噪声/副标题）
    blocks = _classify_headings(blocks, use_llm=use_llm, title=title)

    # Step 5: 后处理（heading 层级重建、编号、空段清理）
    blocks = _post_process(blocks)

    # Step 6: 章节编号断档捞回（引擎把章节标题误判为页眉/header 时：
    # 正文出现 I、II、III、V… 明显断档，从被过滤的页眉池按编号形态捞回；
    # 捞不到候选则由 qc_paper 的断档检查 WARN 提示）
    blocks = _rescue_missing_section_headings(blocks, dropped_noise)

    return blocks


_ROMAN_VALUES = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100}


def _roman_to_int(s: str) -> int | None:
    total, prev = 0, 0
    for ch in reversed(s.upper()):
        v = _ROMAN_VALUES.get(ch)
        if v is None:
            return None
        total += v if v >= prev else -v
        prev = max(prev, v)
    return total if 0 < total <= 30 else None


def _int_to_roman(n: int) -> str:
    vals = [(10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")]
    out = ""
    for v, s in vals:
        while n >= v:
            out += s
            n -= v
    return out


def _rescue_missing_section_headings(blocks: list[ProcessedBlock],
                                     dropped_noise: list[dict]) -> list[ProcessedBlock]:
    """章节罗马编号断档捞回（martins2000 实测：'IV. THE EFFECT OF RADIATION
    BACK-REACTION' 被引擎标成 header 整块过滤）。

    保守判据：一级标题构成明确罗马序列（≥3 个）且断档 ≤3 个；候选块文本以
    缺失编号起首（IV. / IV 形态）、长度像标题；捞回插到下一个编号标题之前。
    无候选 → 不动（由 QC 断档 WARN 提示）。"""
    heads = []
    for i, b in enumerate(blocks):
        if b.kind == "heading" and b.level == 1:
            m = _ROMAN_NUM_RE.match(b.content or "")
            if m:
                rn = _roman_to_int(m.group(0).split(".")[0].split()[0])
                if rn is not None:
                    heads.append((i, rn))
    if len(heads) < 3:
        return blocks
    nums = [rn for _i, rn in heads]
    missing = [n for n in range(min(nums), max(nums) + 1) if n not in nums]
    if not missing or len(missing) > 3:
        return blocks
    for n in sorted(missing, reverse=True):  # 倒序插入，索引不失效
        rn = _int_to_roman(n)
        cand = None
        for b in dropped_noise:
            t = re.sub(r"\s+", " ", (b.get("text") or "").strip())
            if re.match(rf"^{rn}\.?\s+\S", t) and 10 < len(t) < 120:
                cand = (t, b.get("page_idx", 0))
                break
        if cand is None:
            logger.warning(f"  章节编号断档: 缺第 {rn} 节标题（页眉池无候选，未捞回）")
            continue
        # 插入点：候选页（页眉块所在页）正文内容的起始处——该页页锚之后、
        # 第一个属于该页的正文块之前，使标题落在其节内容开头而非下一编号
        # 标题之前（首版插在 V 之前的末尾位置，martins2000 dev 实测纠正）。
        # 找不到可靠插入点 → 不捞（维持断档 + QC WARN，宁缺毋滥）
        ins = None
        for i, b in enumerate(blocks):
            if b.kind == "page_anchor":
                continue
            bpage = getattr(b, "src_page", None)
            if bpage is None and b.kind in ("image", "table", "table_image"):
                bpage = b.page_idx
            if bpage == cand[1]:
                ins = i
                break
        if ins is None:
            logger.warning(f"  章节断档捞回放弃：找不到第 {rn} 节内容的可靠插入点")
            continue
        blocks.insert(ins, ProcessedBlock("heading", content=cand[0], level=1,
                                          src_page=cand[1]))
        logger.info(f"  章节断档捞回: {cand[0][:50]}（页眉池，落于页 {cand[1] + 1} 内容前）")
    return blocks


# 噪声标题模式（被误标为 heading 的出版元数据/栏目名）
_NOISE_HEADING_RES = [
    re.compile(r"^(insights|perspectives|review|reviews|article|communication|editorial|letter|perspective)\b", re.I),
    re.compile(r"^(lithium|sodium|potassium|battery|batteries|chemistry|physics|materials?)\s+(batteries|reviews?|insights?)$", re.I),
    re.compile(r"^\d+\s+\w.*\b(permissions|terms of service|view the article)\b", re.I),
    re.compile(r"^Science\s+\d+\s*\(", re.I),  # "Science 369 (6500), ..." 引用块
]


def _classify_headings(blocks: list, use_llm: bool = False, title: str = "") -> list:
    """标题结构分类：区分真章节 / 图注 / 噪声 / 副标题。

    规则启发式始终运行；use_llm 时再用 LLM 校正（对图注/噪声更准）。
    图注/副标题 → 降为段落（不进 TOC）；噪声 → 移除。
    """
    heading_idxs = [i for i, b in enumerate(blocks) if b.kind == "heading"]
    if not heading_idxs:
        return blocks

    # 收集每个标题的上下文特征
    feat = {}
    for i in heading_idxs:
        ctx_parts = []
        image_nearby = False
        img_before_para = False  # 图片先于任何段落出现（图注特征；真章节标题后通常先有正文）
        for j in range(i + 1, min(i + 4, len(blocks))):
            nb = blocks[j]
            if nb.kind == "heading":
                break
            if nb.kind in ("image", "table_image"):
                if not ctx_parts:
                    img_before_para = True
                image_nearby = True
            elif nb.kind == "paragraph":
                ctx_parts.append(nb.content)
        feat[i] = {
            "text": blocks[i].content,
            "level": blocks[i].level,
            "ctx": " ".join(ctx_parts)[:150],
            "image_nearby": image_nearby,
            "img_before_para": img_before_para,
        }

    # 规则分类
    classes = _rule_classify(blocks, heading_idxs, feat, title)

    # LLM 校正（仅对规则拿不准的标题）
    if use_llm:
        _llm_classify(blocks, heading_idxs, feat, classes, title)

    # 应用分类结果
    drop_idxs = set()
    for i in heading_idxs:
        cls = classes.get(i, "section")
        if cls == "noise":
            drop_idxs.add(i)
        elif cls in ("figure_caption", "subtitle"):
            blocks[i].kind = "paragraph"  # 降为段落，不进 TOC

    if drop_idxs:
        blocks = [b for i, b in enumerate(blocks) if i not in drop_idxs]
    return blocks


def _rule_classify(blocks, heading_idxs, feat, title) -> dict:
    """规则启发式分类标题。"""
    classes = {}
    title_norm = re.sub(r"\s+", " ", title).lower().strip() if title else ""
    seen_first_heading = False

    for i in heading_idxs:
        f = feat[i]
        text = f["text"]
        text_norm = re.sub(r"\s+", " ", text).lower().strip()
        cls = "section"

        # 噪声标题：栏目名/引用元数据
        if any(p.search(text) for p in _NOISE_HEADING_RES):
            cls = "noise"
        # 标题重复（末页引用块常重复论文标题+作者）
        elif title_norm and (text_norm.startswith(title_norm) or title_norm in text_norm) \
                and len(text) > len(title) * 0.8 and seen_first_heading:
            cls = "noise"
        # 图注启发式：标题后紧跟图片（中间无正文段落）→ 很可能是图注
        elif f["image_nearby"] and f["img_before_para"] and not _looks_like_real_section(text):
            cls = "figure_caption"
        # 副标题：紧跟主标题、无编号、描述性的一句话
        elif not seen_first_heading and not _has_number_prefix(text) \
                and f["level"] == 1 and len(text) < 200:
            # 第一个标题是主标题；若其后紧跟另一个 lvl=1 描述句，判为副标题
            pass  # 交给下方 subtitle 逻辑

        # 副标题检测：主标题之后第一个非编号、描述性、带上下文为空的 lvl1 标题
        if cls == "section" and not seen_first_heading and f["level"] == 1 \
                and not _has_number_prefix(text) and not f["ctx"]:
            # 主标题本身（第一个）保留；这里不处理第一个
            pass

        classes[i] = cls
        seen_first_heading = True

    # 副标题：主标题（第一个 heading）之后、正文之前，紧跟的第二个 lvl1 描述性标题
    if len(heading_idxs) >= 2:
        first_i = heading_idxs[0]
        second_i = heading_idxs[1]
        f2 = feat[second_i]
        if (classes.get(first_i) == "section" and f2["level"] == 1
                and not _has_number_prefix(f2["text"])
                and not _looks_like_real_section(f2["text"])):
            classes[second_i] = "subtitle"

    return classes


def _looks_like_real_section(text: str) -> bool:
    """判断标题是否像真章节名（有编号，或是常见章节词）。"""
    if _has_number_prefix(text):
        return True
    t = re.sub(r"\s+", " ", text).lower().strip().rstrip(":.")
    real_section_words = {
        "abstract", "摘要", "introduction", "background", "methods", "methodology",
        "experimental", "results", "results and discussion", "discussion",
        "conclusion", "conclusions", "summary", "references", "references and notes",
        "acknowledgments", "acknowledgements", "supplementary materials",
        "supporting information", "data availability", "author contributions",
        "competing interests", "materials and methods", "outlook", "perspective",
    }
    return t in real_section_words


def _llm_classify(blocks, heading_idxs, feat, classes, title) -> None:
    """用 LLM 校正标题分类（就地修改 classes）。"""
    try:
        from openai import OpenAI
        import config
    except ImportError:
        return
    if not getattr(config, "DEEPSEEK_API_KEY", None):
        return

    # 只把规则判为 section 但可疑的（无编号、非典型章节词）交给 LLM
    # 跳过第一个标题（论文主标题，始终保留为 heading）
    first_heading = heading_idxs[0] if heading_idxs else None
    suspect = []
    for i in heading_idxs:
        if i == first_heading:
            continue
        if classes.get(i) != "section":
            continue
        text = feat[i]["text"]
        if _has_number_prefix(text) or _looks_like_real_section(text):
            continue
        suspect.append(i)
    if not suspect:
        return

    listing = []
    for k, i in enumerate(suspect):
        f = feat[i]
        listing.append(f"[{k}] 标题: {f['text'][:80]}\n    后续内容: {f['ctx'][:100]}\n    标题后紧跟图片（中间无正文段落）: {'是' if f['img_before_para'] else '否'}")

    prompt = (
        f"以下是从一篇学术论文（标题：{title[:80]}）中检测到的若干标题，请判断每个是哪种类型。\n"
        "类型说明：\n"
        "- section: 真正的章节标题（如 Introduction/Results/某方法名）\n"
        "- figure_caption: 图片/图表的说明标题（描述某张图，常紧跟图片）\n"
        "- noise: 栏目名、引用元数据、重复标题等噪声\n"
        "- subtitle: 论文主标题的副标题/dek\n\n"
        "待分类标题：\n" + "\n".join(listing) + "\n\n"
        '返回严格 JSON：{"结果": [{"idx": 0, "type": "figure_caption"}, ...]}，不要其他文字。'
    )
    try:
        client = OpenAI(api_key=config.DEEPSEEK_API_KEY, base_url=config.DEEPSEEK_BASE_URL)
        resp = client.chat.completions.create(
            model=config.DEEPSEEK_MODEL,
            messages=[
                {"role": "system", "content": "你是学术论文结构分析专家。只返回 JSON。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.1,
            max_tokens=1000,
        )
        content = resp.choices[0].message.content.strip()
        content = re.sub(r"^```(?:json)?\s*", "", content)
        content = re.sub(r"\s*```$", "", content)
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            data = json.loads(re.sub(r'\\(?!["\\/bfnrtu])', r'\\\\', content))
        items = data.get("结果", data.get("results", []))
        valid = {"section", "figure_caption", "noise", "subtitle"}
        for item in items:
            k = item.get("idx")
            typ = item.get("type")
            if isinstance(k, int) and 0 <= k < len(suspect) and typ in valid:
                classes[suspect[k]] = typ
        logger.info(f"  LLM 标题分类校正: {len(items)} 项")
    except Exception as e:
        logger.warning(f"  LLM 标题分类失败（保留规则结果）: {e}")


def _build_ir(blocks: list[dict], images_dir: str) -> list[ProcessedBlock]:
    """将过滤后的块转换为 IR"""
    result = []
    current_page = -1
    in_references = False
    skip_related = False  # 跳过 "You may also like" 推荐列表项
    last_footnote = None  # 最近一条编号脚注块（跨页断注续行归并用）

    for block in blocks:
        page_idx = block.get("page_idx", 0)
        block_type = block["type"]
        text = _normalize_inline(block.get("text", "").strip())

        # 页码锚点
        if page_idx != current_page:
            current_page = page_idx
            result.append(ProcessedBlock("page_anchor", content=str(page_idx + 1), page_idx=page_idx))

        # 跳过空文本块（非图片/公式/表格/列表——list 块文本在 list_items 数组里，
        # 没有 text 字段，曾被此处静默丢弃：forecast 整个文献区丢失事故）
        if not text and block_type not in ("image", "chart", "equation", "table", "list"):
            continue

        # --- Heading ---
        if block_type == "text" and block.get("text_level"):
            # 跳过噪声标题（"You may also like" / 期刊名等出版元数据）
            hnorm = re.sub(r"\s+", " ", text).lower().rstrip(":.")
            if any(nh in hnorm for nh in _NOISE_HEADINGS):
                if "you may also like" in hnorm:
                    skip_related = True
                continue
            # 遇到真实标题，结束推荐列表跳过
            skip_related = False
            # 检查是否进入参考文献区
            if _is_reference_heading(text):
                in_references = True
            else:
                in_references = False
            # text_level 仅作弱提示；真实层级由 _assign_heading_levels 重建
            result.append(ProcessedBlock("heading", content=text, level=block["text_level"],
                                         src_page=page_idx))
            continue

        # --- 页脚注（作者单位/正文脚注） ---
        # 落成独立 footnote 块：不参与段落合并（页底注释与正文拼读会串文），
        # renderer 以 Pandoc 脚注定义形态（[^N]: ...）输出。
        # 编号形态（引擎归一后的实测形态）："$^{1}$ For ..." / "6 Note that ..." /
        # "4Note that ..."（数字粘连首词）；符号标记（\* / $^{\dagger}$ 作者
        # 邮箱脚注）无编号，保持原样输出。
        # 无编号非符号块判为续行并入最近一条编号脚注——跨页断注续段与编号块
        # 之间可能隔着整页正文（forecast 实测脚注 5 跨页续段），不按紧邻判定；
        # 保守闸：续段小写/标点起首，或上条脚注句未完结
        if block_type == "page_footnote":
            if text:
                n, stripped = _parse_footnote_num(text)
                if n is None and not _FOOTNOTE_SYMBOL_RE.match(text) \
                        and last_footnote is not None:
                    prev_t = last_footnote.content.rstrip()
                    if re.match(r"^[a-z),;.]", text) \
                            or not re.search(r'[.!?…]["\'\)\]]*\s*$', prev_t):
                        last_footnote.content = prev_t + " " + text
                        continue
                nb = ProcessedBlock("footnote",
                                    content=stripped if n is not None else text,
                                    src_page=page_idx)
                nb.note_num = n
                result.append(nb)
                if n is not None:
                    last_footnote = nb
            continue

        # --- 列表块 ---
        # list 块携 list_items 数组（引擎/后端 schema 差异形态；forecast 实测
        # 整个文献区是 list+sub_type:ref_text，曾整块静默丢弃——零丢失事故）。
        # ref_text 或处于文献区：每个 item 一条 reference；其他 sub_type
        # （itemize/enumerate 正文列表，语料未见）逐条落成段落，文本保底
        if block_type == "list":
            for item in block.get("list_items") or []:
                it = _normalize_inline((item or "").strip())
                if not it:
                    continue
                if block.get("sub_type") == "ref_text" or in_references:
                    result.append(ProcessedBlock("reference", content=it,
                                                 src_page=page_idx))
                else:
                    result.append(ProcessedBlock("paragraph", content=it,
                                                 src_page=page_idx))
            continue

        # --- 参考文献 ---
        if block_type == "ref_text" or in_references:
            if text:
                result.append(ProcessedBlock("reference", content=text, src_page=page_idx))
            continue

        # --- 公式 ---
        if block_type == "equation":
            # 编号拆行伪影去重（多 \tag → 单 \tag，见 _dedup_equation_tags）
            text = _dedup_equation_tags(text)
            # 确保 $$ 包裹
            eq_text = text
            if not eq_text.startswith("$$"):
                eq_text = f"$$\n{eq_text}\n$$"
            result.append(ProcessedBlock("equation", content=eq_text, src_page=page_idx))
            continue

        # --- 表格 ---
        if block_type == "table":
            # MinerU 的 table_body 是现成 HTML（含 colspan/rowspan），
            # 契约约定复杂表格直接用 HTML <table>，原样透传；
            # 但先拆分 MinerU 把正文/标题合并进表格的版面缺陷单元格
            body = (block.get("table_body") or "").strip()
            if body:
                # 表体内同样可能有断裂上下标与上标化引文（HTML 表格不经 _normalize_inline）
                body = _normalize_citation_sup(_repair_script_frac(body))
            caption = _extract_table_caption(block)
            if not caption or _is_junk_caption(caption):
                # caption 缺失/是垃圾时，尝试绑定紧邻前驱的 "Table N" 段落为表注
                # （RSC 版式：表注常被页脚噪声粘连成独立文本块）
                if result and result[-1].kind == "paragraph" \
                        and re.match(r"^Table\s+\d", result[-1].content):
                    caption = result.pop().content
            if body:
                body, rescued = _rescue_prose_cells(body, page_idx)
                if body:
                    result.append(ProcessedBlock(
                        "table", content=body, caption=caption, page_idx=page_idx,
                        src_page=page_idx,
                    ))
                result.extend(rescued)
            elif block.get("img_path"):
                # 无 HTML 表体时退化为图片（table_image 类型，不进图组编号、
                # 保留表注，避免被误编成 "Figure N"）
                cap = caption if not _is_junk_caption(caption) else ""
                result.append(ProcessedBlock(
                    "table_image",
                    content=cap or "Table",
                    caption=cap,
                    img_src=block["img_path"],
                    page_idx=page_idx,
                    src_page=page_idx,
                ))
            continue

        # --- 图片/图表 ---
        if block_type in ("image", "chart"):
            img_path = block.get("img_path", "")
            # 提取原始 caption（图组编号由 _assign_figure_numbers 统一处理）
            caption = _extract_caption(block)
            # caption 混入正文（MinerU 把图下方文字栏并进 image_caption）：
            # 小写起首的长句；或无 Fig/Table 等编号前缀的多句长段（真图注通常
            # 1-2 句或带编号前缀）→ 拆出为段落接回阅读流
            body_text = ""
            if caption:
                lower_start = len(caption) > 40 and re.match(r"^[a-z]", caption)
                prose_like = (
                    len(caption) > 200 and caption.count(". ") >= 3
                    and not re.match(r"^(fig(?:ure)?\.?|table|scheme|chart|supplement|图|表)\b",
                                     caption, re.I))
                if lower_start or prose_like:
                    body_text, caption = caption, ""
            result.append(ProcessedBlock(
                "image",
                content=caption,
                caption=caption,
                img_src=img_path,
                img_new_name="",  # 稍后由 _assign_figure_numbers 填充
                page_idx=page_idx,
                bbox=block.get("bbox"),
                src_page=page_idx,
            ))
            if body_text:
                result.append(ProcessedBlock("paragraph", content=body_text,
                                             page_idx=page_idx, src_page=page_idx))
            continue

        # --- 普通文本段落 ---
        if block_type == "text" and text:
            # 跳过推荐列表项（"You may also like" 之后的 "- " 项）
            if skip_related and re.match(r"^\\?-\s", text):
                continue
            # 清洗出版信息噪声（纯噪声返回 None 丢弃；粘连块剥离前缀）
            cleaned = _clean_paragraph(text)
            if cleaned is None:
                continue
            text = cleaned
            # 三级标题形态（RSC 综述实测：x.y.z 短标题与正文粘连在同一块，
            # 引擎不给 text_level）：编号三位点号 + 短标题句（≤80 字符、句号收尾）
            # + 正文接续（大写/行内公式起首 ≥40 字符；wang2024routes 实测 5.6.1
            # 正文以 $Na_{x}...$ 起首，纯大写类判定会漏拆）→ 拆为三级标题 + 正文段
            h3_match = re.match(
                r"^(\d+\.\d+\.\d+)\.?\s+([A-Z][^.\n]{4,80}?)\.\s+([A-Z($].{40,})$",
                text, re.S)
            if h3_match and _DOTTED_NUM_RE.match(text):
                h3_title = f"{h3_match.group(1)} {h3_match.group(2).strip()}"
                result.append(ProcessedBlock("heading", content=h3_title, level=3,
                                             src_page=page_idx))
                result.append(ProcessedBlock("paragraph",
                                             content=h3_match.group(3).strip(),
                                             src_page=page_idx))
                continue
            # 检测是否是摘要段（"Abstract: ..." / "Conspectus: ..." 格式）
            abstract_match = re.match(r"^(?:abstract|conspectus)[:\.\s]+(.+)", text, re.IGNORECASE | re.DOTALL)
            if abstract_match:
                # 先插入 Abstract heading
                result.append(ProcessedBlock("heading", content="Abstract", level=1,
                                             src_page=page_idx))
                result.append(ProcessedBlock("paragraph", content=abstract_match.group(1).strip(),
                                             src_page=page_idx))
                continue
            # 中文摘要（"摘 要 ..." / 允许机构行等短前缀粘连："（xx大学...) 摘 要 ..."）
            # 前缀以汉字/字母结尾说明"摘要"在句中（如"本文摘要"），不拆
            zh_match = re.match(r"^(.*?)\s*(摘\s*要)[:：\s]+(.+)", text, re.DOTALL)
            if zh_match and len(zh_match.group(1)) < 200 \
                    and not re.search(r"[A-Za-z0-9\u4e00-\u9fff]$", zh_match.group(1)):
                prefix = zh_match.group(1).strip()
                if prefix:
                    result.append(ProcessedBlock("paragraph", content=prefix,
                                                 src_page=page_idx))
                result.append(ProcessedBlock("heading", content="摘要", level=1,
                                             src_page=page_idx))
                result.append(ProcessedBlock("paragraph", content=zh_match.group(3).strip(),
                                             src_page=page_idx))
            else:
                result.append(ProcessedBlock("paragraph", content=text, src_page=page_idx))

    _ensure_references_heading(result)
    return result


def _ensure_references_heading(blocks: list[ProcessedBlock]) -> None:
    """参考文献区无标题时补一级 heading（约 1/4 论文的引擎产物缺该标题——
    裸列的参考文献会被 SageRead 按 heading 切片时并入前一章节，且契约要求
    无编号固定段有对应层级 #）。已有参考文献标题则不动；
    "References" 被当成正文段落在前的，升级为标题而非重复插入。
    """
    first_ref = next((i for i, b in enumerate(blocks) if b.kind == "reference"), None)
    if first_ref is None:
        return
    for b in blocks[:first_ref]:
        if b.kind == "heading" and _is_reference_heading(b.content):
            return
    # 紧邻前驱（跳过页码锚点）是 "References" 类段落 → 升级为标题
    j = first_ref - 1
    while j >= 0 and blocks[j].kind == "page_anchor":
        j -= 1
    if j >= 0 and blocks[j].kind == "paragraph" and _is_reference_heading(blocks[j].content):
        blocks[j].kind = "heading"
        if blocks[j].content.isupper():
            blocks[j].content = blocks[j].content.title()  # "REFERENCES" → "References"
        return
    # 否则在第一条参考文献前插入合成标题（页码锚点之后，保住锚点与页的相对位置）
    blocks.insert(first_ref, ProcessedBlock("heading", content="References"))


def _is_reference_heading(text: str) -> bool:
    """判断是否是参考文献标题"""
    lower = text.lower().rstrip(":.")
    return lower in ("references", "references and notes", "bibliography", "works cited")


# 脚注符号标记（作者邮箱等无编号脚注）：\* / † / $^{\dagger}$ 等
_FOOTNOTE_SYMBOL_RE = re.compile(
    r"^\s*(?:\\[\*†‡§¶#]|[\*†‡§¶#]|"
    r"\$\^\{?\\?(?:dagger|ddagger|ast|star|diamond|ast)\"?)", re.I)


def _parse_footnote_num(text: str) -> tuple[int | None, str]:
    """解析脚注编号前缀，返回 (编号, 去编号后的内容)；无编号 → (None, 原文)。

    覆盖引擎归一后的实测形态：$^{1}$/$^{1} 上标式、"6 Note that"（编号+空格）、
    "4Note that"/"1GWs"（编号粘连首词）。防误切：编号后须接大写字母
    （"2020 was" / "3.5 σ" 之类量值不是脚注编号）。
    """
    m = re.match(r"^\s*\$\^\{?(\d{1,2})\}?\$?\s*(?=\S)", text)
    if m:
        return int(m.group(1)), text[m.end():].strip()
    m = re.match(r"^(\d{1,2})\s*(?=[A-Z])", text)
    if m:
        return int(m.group(1)), text[m.end():].strip()
    return None, text


def _extract_caption(block: dict) -> str:
    """从图片/图表块提取 caption"""
    # 优先 chart_caption，再 image_caption
    captions = block.get("chart_caption", []) or block.get("image_caption", [])
    if not captions:
        return ""

    # 合并所有 caption 行
    parts = []
    for cap in captions:
        cap = _normalize_inline(cap.strip())
        # 跳过单字母标记（如 "A", "B", "C", "D"）
        if len(cap) <= 2 and cap.isalpha():
            continue
        parts.append(cap)

    # 面板标签乱序在真图注之前（forecast 实测：chart_caption 列表把
    # "(c) p=..." 面板标签行排在 "Figure 5: ..." 真图注行之前，乱序拼接会让
    # 图注首词判定（prose_like 降为正文/组界识别）全错）：恰一行以图编号
    # 起首且不在首位 → 提到最前，面板标签保留在真图注之后（文本零丢失）
    if len(parts) > 1:
        marked = [k for k, p in enumerate(parts) if _FIG_CAP_START_RE.match(p)]
        if len(marked) == 1 and marked[0] != 0:
            k = marked[0]
            parts = [parts[k]] + parts[:k] + parts[k + 1:]
    return " ".join(parts)


# 图注行起首形态（"Figure 3: " / "FIG. 5. " / "Fig. 12.4: "；面板字母
# "Fig. 3a" 与句中引用 "Fig. 7)." 因编号后不跟 ". "/": " 而不命中）
_FIG_CAP_START_RE = re.compile(
    r"^(?:Fig(?:ure)?\.?|FIG\.?)\s*\d+(?:\.\d+)*\s*[\.\:]\s", re.I)


def _extract_table_caption(block: dict) -> str:
    """从表格块提取 caption（table_caption 列表合并；剥离页脚噪声前缀与续表标记）"""
    captions = block.get("table_caption") or []
    parts = []
    for c in captions:
        # 剥离 RSC 页脚前缀（"Published on ... Downloaded on ..."）
        c = re.sub(r"^(?:(?:Published|Downloaded) on\s+[^.]*\.\s*)+", "", c.strip())
        # 去续表标记（"Table 1 (Contd.)" → "Table 1"）
        c = _TABLE_CONT_RE.sub("", c).strip()
        if c and not _is_junk_caption(c):
            parts.append(c)
    return " ".join(parts)


# ============================================================
# 跨页表格合并（规则框架借鉴 MinerU-Popo 的 table_merge_filter /
# table_merge_utils，按我们 content_list 结构适配；不引入模型）
# ============================================================

# 续表标记（caption 含这些即判为续表）
_TABLE_CONT_RE = re.compile(
    r"\(?\s*(continued|contd\.?|cont\.?|cont'd|续表|续上表|接续)\s*\)?\s*$", re.I)

# 垃圾 caption（页眉页脚被误当表注）：按无 caption 处理
_TABLE_JUNK_CAPTION_RES = [
    re.compile(r"^(published|downloaded|received|accepted)\b", re.I),
    re.compile(r"^https?://", re.I),
    re.compile(r"^view (article|this)", re.I),
    re.compile(r"^doi\b", re.I),
]


def _table_rows_html(html_str: str) -> list:
    return re.findall(r"<tr[^>]*>.*?</tr>", html_str, re.S | re.I)


def _row_cells_text(row_html: str) -> list:
    """一行的单元格 [(归一化文本, colspan)]，用于表头去重比对"""
    cells = []
    for cell in re.findall(r"<t[dh][^>]*>.*?</t[dh]>", row_html, re.S | re.I):
        text = unescape(re.sub(r"<[^>]+>", "", cell))
        text = re.sub(r"\s+", "", text).lower()
        m = re.search(r'colspan="(\d+)"', cell[: cell.index(">")])
        cells.append((text, int(m.group(1)) if m else 1))
    return cells


def _table_col_count(html_str: str) -> int:
    """表格总列数（colspan 感知，取各行最大值）"""
    best = 0
    for row in _table_rows_html(html_str):
        best = max(best, sum(cs for _, cs in _row_cells_text(row)))
    return best


def _table_num(caption: str) -> str | None:
    m = re.search(r"\b(?:table|tab\.?|exhibit|表)\s*[.:]?\s*(\d+(?:\.\d+)*)",
                  caption, re.I)
    return m.group(1) if m else None


def _is_junk_caption(cap: str) -> bool:
    return len(cap) > 200 or any(p.search(cap) for p in _TABLE_JUNK_CAPTION_RES)


def _can_merge_tables(t1: dict, t2: dict) -> bool:
    """判断相邻两页边界的两个表格是否同一逻辑表（保守：拿不准就拒）"""
    cap1 = _extract_table_caption(t1)
    cap2 = _extract_table_caption(t2)
    if _is_junk_caption(cap1):
        cap1 = ""
    if _is_junk_caption(cap2):
        cap2 = ""

    if cap2:
        if not _TABLE_CONT_RE.search(cap2):
            # 无续表标记：要求编号一致（跨页重复表头/caption 的续表）
            n1, n2 = _table_num(cap1), _table_num(cap2)
            if n1 is None or n2 is None or n1 != n2:
                return False
    # 列数兼容（Popo 判据之一；不等即拒绝，不尝试对齐）
    c1 = _table_col_count(t1.get("table_body") or "")
    c2 = _table_col_count(t2.get("table_body") or "")
    if c1 and c2 and c1 != c2:
        return False
    return True


def _exec_table_merge(t1: dict, t2: dict) -> None:
    """把 t2 的行并入 t1（重复表头行去重；caption 保留 t1 的）"""
    body1 = t1.get("table_body") or ""
    body2 = t2.get("table_body") or ""
    rows1 = _table_rows_html(body1)
    rows2 = _table_rows_html(body2)

    # 表头去重：t2 首行与 t1 首行同构则丢弃（最多查 3 行）
    if rows1:
        h1 = _row_cells_text(rows1[0])
        dropped = 0
        while rows2 and dropped < 3 and _row_cells_text(rows2[0]) == h1:
            rows2.pop(0)
            dropped += 1

    merged = re.sub(r"</table>\s*$", "", body1, flags=re.I) + "".join(rows2) + "</table>"
    t1["table_body"] = merged
    if t2.get("table_footnote"):
        t1["table_footnote"] = (t1.get("table_footnote") or []) + t2["table_footnote"]


def _merge_cross_page_tables(blocks: list[dict]) -> list[dict]:
    """合并跨页续表：块序上相邻的两个表格，跨页且中间无正文块则尝试合并。

    候选只看"下一个有表体的表格"（中间隔着无表体的图片页不阻断），
    链式续表（跨 3+ 页）反复合并直到无候选。
    """
    def _is_table(b):
        return b.get("type") == "table" and (b.get("table_body") or "").strip()

    def _header_cells(t: dict) -> set:
        """表格首行单元格的归一化文本集合（与 _row_cells_text 同判据）"""
        rows = _table_rows_html(t.get("table_body") or "")
        return {txt for txt, _ in _row_cells_text(rows[0])} if rows else set()

    def _has_text_between(i1, i2, t1, t2):
        # 中间的正文块会阻断合并；出版噪声块（会被 _clean_paragraph 丢弃的）不算；
        # 续页表头被引擎拆出的碎片文本块（与任一表格首行单元格逐字相同的短块）也不算
        cells = _header_cells(t1) | _header_cells(t2)
        for b in blocks[i1 + 1:i2]:
            if b.get("type") in ("text", "list", "ref_text"):
                t = (b.get("text") or "").strip()
                if not t:
                    continue
                if len(t) <= 40 and re.sub(r"\s+", "", t).lower() in cells:
                    continue
                if _clean_paragraph(t) is not None:
                    return True
        return False

    changed = True
    while changed:
        changed = False
        tidx = [i for i, b in enumerate(blocks) if _is_table(b)]
        for i1, i2 in zip(tidx, tidx[1:]):
            t1, t2 = blocks[i1], blocks[i2]
            # 同页相邻的是两张表，不合；必须跨页
            if t2.get("page_idx", 0) <= t1.get("page_idx", 0):
                continue
            if _has_text_between(i1, i2, t1, t2):
                continue
            if _can_merge_tables(t1, t2):
                _exec_table_merge(t1, t2)
                blocks[i2]["type"] = "_merged"
                changed = True
                break  # 重扫（链式）

    return [b for b in blocks if b.get("type") != "_merged"]


# 表格内正文单元格判据：≥200 字符且含英文句子结构（正常数据表单元格不会命中）
_PROSE_CELL_MIN = 200


def _rescue_prose_cells(html_str: str, page_idx: int = 0) -> tuple[str, list]:
    """拆分 MinerU 把正文/标题合并进表格 HTML 的版面缺陷单元格。

    实测案例：双栏页左栏缩写表 + 右栏 "1 Introduction" 全部正文被并进
    同一 <table>（正文藏在 colspan/rowspan 大单元格里），重解析也一样。
    保守判据（正常数据表不会命中）：
      - 正文单元格：纯文本 ≥200 字符且含句子结构 → 拆出为段落
      - 编号标题单元格：跨列、短文本、点号编号前缀（"1 Introduction"）→ 拆出为标题
    返回 (清理后的 table HTML, 拆出的 ProcessedBlock 列表)。
    """
    rows = re.findall(r"<tr[^>]*>.*?</tr>", html_str, re.S | re.I)
    if not rows:
        return html_str, []

    kept_rows = []
    rescued: list[ProcessedBlock] = []
    for row in rows:
        kept_cells = []
        for cell in re.findall(r"<t[dh][^>]*>.*?</t[dh]>", row, re.S | re.I):
            text = unescape(re.sub(r"<[^>]+>", "", cell))
            text = re.sub(r"\s+", " ", text).strip()
            if len(text) >= _PROSE_CELL_MIN and re.search(r"[a-zA-Z]\. [A-Z(]", text):
                rescued.append(ProcessedBlock("paragraph", content=text, page_idx=page_idx,
                                              src_page=page_idx))
                continue
            attrs = cell[: cell.index(">")]
            m = re.search(r'colspan="(\d+)"', attrs)
            colspan = int(m.group(1)) if m else 1
            if colspan >= 2 and 0 < len(text) < 120 and _DOTTED_NUM_RE.match(text):
                rescued.append(ProcessedBlock("heading", content=text, level=1, page_idx=page_idx,
                                              src_page=page_idx))
                continue
            kept_cells.append(cell)
        if kept_cells:
            kept_rows.append("<tr>" + "".join(kept_cells) + "</tr>")

    clean = "<table>" + "".join(kept_rows) + "</table>" if kept_rows else ""
    return clean, rescued


def _assign_table_numbers(blocks: list[ProcessedBlock]) -> None:
    """表注缺号补号：caption 无编号且正文存在未被占用的 'Table N' 引用时，
    按块序分配最小的悬空引用编号。

    引擎偶发丢表注的标号行（ernst2007 实证）。但无引用引用的表不补——
    术语符号表（nomenclature）本就不编号（madler2001 实证：无编号大表是
    符号表而非 Table 2，误补反而错标）。
    """
    used = set()
    for b in blocks:
        if b.kind in ("table", "table_image"):
            n = _table_num(b.caption or "")
            if n:
                used.add(n)
    cited: set[str] = set()
    for b in blocks:
        if b.kind in ("paragraph", "heading"):
            for m in re.finditer(r"\bTable\s+(\d+)", b.content or ""):
                cited.add(m.group(1))
    pool = sorted((int(n) for n in cited - used))
    for b in blocks:
        if b.kind not in ("table", "table_image"):
            continue
        cap = (b.caption or "").strip()
        if _table_num(cap):
            continue
        if not pool:
            break  # 无悬空引用——可能是术语表，保持无编号
        n = pool.pop(0)
        b.caption = f"Table {n}: {cap}" if cap else f"Table {n}"
        if b.kind == "table_image" and not (b.content or "").strip():
            b.content = b.caption
        logger.info(f"  表注补号: Table {n}（{cap[:40]}）")


def _relocate_stray_reference_paragraphs(blocks: list[ProcessedBlock]) -> list[ProcessedBlock]:
    """参考文献区混入正文段的重定位（双栏阅读顺序错乱）。

    引擎按栏产出时可能把正文末段嵌进文献序列（正确顺序：左栏正文尾→右栏
    正文尾→左栏文献→右栏文献；错乱产物：左栏正文→左栏文献→右栏正文→右栏
    文献，宇宙弦实测）。文献区里一切文本块在 _build_ir 都落成 reference，
    故 paragraph 块出现在文献序列中本身就是阅读顺序错误的铁证。
    保守判据（全满足才搬移，搬到最后一个参考文献标题之前）：
      - 候选块 kind == "paragraph"；
      - 后邻（跳 page_anchor）是带编号的 reference 条目；
      - 前邻是带编号条目或参考文献标题本身；
      - 内容是完整正文句形态（≥100 字符且 ≥10 词；条目形态/碎片不搬）。
    reference 块一律不动——APA 无编号条目流与条目续行不受任何影响。
    """
    ref_h = None
    for i, b in enumerate(blocks):
        if b.kind == "heading" and _is_reference_heading(b.content or ""):
            ref_h = i  # 取最后一个参考文献标题（正文里的同名提及被后者覆盖）
    if ref_h is None:
        return blocks

    def _num(b) -> bool:
        return b.kind == "reference" and _REF_NUM_RE.match(b.content or "") is not None

    def _prev_non_anchor(i):
        j = i - 1
        while j > ref_h and blocks[j].kind == "page_anchor":
            j -= 1
        return j

    def _next_non_anchor(i):
        j = i + 1
        while j < len(blocks) and blocks[j].kind == "page_anchor":
            j += 1
        return j if j < len(blocks) else None

    out: list[ProcessedBlock] = []
    moved: list[ProcessedBlock] = []
    for i, b in enumerate(blocks):
        if i <= ref_h or b.kind != "paragraph":
            out.append(b)
            continue
        t = (b.content or "").strip()
        if len(t) < 100 or len(t.split()) < 10:
            out.append(b)
            continue
        pi = _prev_non_anchor(i)
        ni = _next_non_anchor(i)
        prev_ok = pi == ref_h or (pi is not None and _num(blocks[pi]))
        next_ok = ni is not None and _num(blocks[ni])
        if prev_ok and next_ok:
            moved.append(b)
            logger.info(f"  文献区混入正文段重定位: {t[:50]}...")
            continue
        out.append(b)
    if not moved:
        return blocks
    # 插入点：参考文献标题之前（保留标题前的 page_anchor 原位——
    # 插到"标题及其前置锚点"整体之前，正文尾之后）
    ins = out.index(blocks[ref_h])
    while ins > 0 and out[ins - 1].kind == "page_anchor":
        ins -= 1
    return out[:ins] + moved + out[ins:]


def _post_process(blocks: list[ProcessedBlock]) -> list[ProcessedBlock]:
    """后处理：层级重建、段落合并、编号、清理"""
    # 1. 合并段落碎片
    blocks = _merge_paragraph_fragments(blocks)

    # 2. 重建 heading 层级（编号前缀解析 + 形状栈）
    _assign_heading_levels(blocks)

    # 2.5 游离编号图注处理（Blood/PNAS 式 "Figure N. ..." 以正文块存在）
    blocks = _split_figure_legends(blocks)

    # 3. 图组编号（子图归并到所属 Figure；含游离图注绑回）
    _assign_figure_numbers(blocks)

    # 3.5 表图（table_image）命名：table1/table2...，与 figN 不冲突
    for i, b in enumerate((b for b in blocks if b.kind == "table_image"), 1):
        ext = Path(b.img_src).suffix if b.img_src else ".png"
        b.img_new_name = f"table{i}{ext}"

    # 3.6 表注缺号补号：caption 无 'Table N' 前缀时分配最小未占用编号
    # （引擎偶发丢表注标号行，QC 引用对账会发现缺号——ernst2007 实证）
    _assign_table_numbers(blocks)

    # 4. 为无编号体系的论文补编号
    _add_numbering(blocks)

    # 4.5 参考文献区混入正文段重定位（双栏阅读顺序错乱：
    # 左栏正文→左栏文献→右栏正文→右栏文献 的引擎产物会把正文末段
    # 嵌进文献序列里——宇宙弦实测）。只搬移 paragraph 块（文献区里
    # paragraph 本就是异常——该区一切文本块在 _build_ir 都落成
    # reference），且要求后邻是编号条目、前邻是编号条目或参考文献
    # 标题本身；reference 块（含 APA 无编号条目流与条目续行）一律不动
    blocks = _relocate_stray_reference_paragraphs(blocks)

    # 5. 清理连续空锚点 + 首尾锚点
    result = []
    last_was_anchor = False
    for block in blocks:
        if block.kind == "page_anchor":
            if last_was_anchor:
                continue
            last_was_anchor = True
            result.append(block)
            continue
        last_was_anchor = False
        result.append(block)

    while result and result[0].kind == "page_anchor":
        result.pop(0)
    while result and result[-1].kind == "page_anchor":
        result.pop()

    return result


# 编号前缀正则
_DOTTED_NUM_RE = re.compile(r"^(\d+(?:\.\d+)*)\.?\s+(.+)")     # "2 Foo" / "3.1 Bar" / "3.1.1 Baz"
_PAREN_NUM_RE = re.compile(r"^[（(](\d+)[）)]\.?\s+(.+)")        # "(1) Foo" / "（1） Foo"
# 罗马数字章节编号（APS 风格 "I. INTRODUCTION"）及其下的字母编号（"A. Motivation"）
# 枚举 I–XV：单字母 C/D/L 不匹配——它们在论文里几乎总是字母小节标签（A. B. C.），
# 作为罗马数字（100/500/50）的章节编号不会出现
_ROMAN_NUM_RE = re.compile(
    r"^(?:I|II|III|IV|V|VI|VII|VIII|IX|X|XI|XII|XIII|XIV|XV)\.\s+\S")
_ALPHA_NUM_RE = re.compile(r"^[A-Z]\.\s+\S")

# 图注中的图编号正则（"Fig. 1" / "Figure 3" / 章节式小数编号 "Fig. 12.4"）
# 尾部负向断言只排数字："Figure 5."（点后随标题）与 "(Figure 2A)"（图版字母）都要命中
_FIG_NUM_RE = re.compile(r"\bFig(?:ure|\.)?\s*(\d+(?:\.\d+)*)(?!\d)", re.IGNORECASE)


def _clean_spaced_heading(text: str) -> str:
    """归一化空格拆字标题："A B S T R A C T" → "Abstract"。

    仅处理每个字母间都用空格隔开的样式（全部为单字符 token），
    避免误伤正常的多单词标题。
    """
    if re.match(r"^\s*[A-Za-z](?:\s+[A-Za-z])+\s*$", text):
        word = re.sub(r"\s+", "", text)
        return word.capitalize()
    return text


# 标题纯文本化用的上/下标 unicode 映射（仅覆盖映射干净的字符：
# 数字 + 运算符 + n/i 上标；含其他字符的脚本整体退化纯文本，不混搭）
_SUP_CHARS = "0123456789+-=()ni"
_SUB_CHARS = "0123456789+-=()"
_SUP_TRANS = str.maketrans(_SUP_CHARS, "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿⁱ")
_SUB_TRANS = str.maketrans(_SUB_CHARS, "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎")

# 行内公式段（排除 $$ 显示公式与跨行段）
_INLINE_MATH_RE = re.compile(r"(?<!\$)\$(?!\$)([^$\n]+?)\$(?!\$)")
# 标题公式里允许剥壳的文本命令
_HEADING_TEXT_CMD_RE = re.compile(
    r"\\(?:mathrm|mathit|mathbf|mathsf|mathnormal|text|operatorname)"
    r"\{([^{}]*)\}")


def _plain_script(content: str, chars: str, trans: dict) -> str:
    """脚本内容 → unicode 上/下标；有字符无映射时整段退化纯文本。"""
    if content and all(c in chars for c in content):
        return content.translate(trans)
    return content


def _heading_plain_math(text: str) -> str:
    r"""标题内联公式 → 纯文本（TOC/锚点/检索共用 heading 文本，不能夹带
    原始 LaTeX——SageRead TOC 以渲染 DOM 的 textContent 取标题，
    "$^{+}$" 会漏成 "+^{+}" 残留）。

    只转换可被"文本命令剥壳 + 上下标 unicode 化"完全归约的 $...$ 段
    （$^{+}$ → ⁺、$_{2}$ → ₂、$\mathrm{Na}$ → Na）；
    含未识别命令/结构的段保留 $...$ 原样（正文 KaTeX 仍能渲染，好过硬转出垃圾）。
    """
    if "$" not in text:
        return text

    def _conv(m: re.Match) -> str:
        inner = m.group(1).strip()
        prev = None
        while prev != inner:
            prev = inner
            inner = _HEADING_TEXT_CMD_RE.sub(r"\1", inner)
        inner = re.sub(r"\^\{([^{}]*)\}",
                       lambda s: _plain_script(s.group(1), _SUP_CHARS, _SUP_TRANS), inner)
        inner = re.sub(r"_\{([^{}]*)\}",
                       lambda s: _plain_script(s.group(1), _SUB_CHARS, _SUB_TRANS), inner)
        inner = re.sub(r"\^([^\s{])",
                       lambda s: _plain_script(s.group(1), _SUP_CHARS, _SUP_TRANS), inner)
        inner = re.sub(r"_([^\s{])",
                       lambda s: _plain_script(s.group(1), _SUB_CHARS, _SUB_TRANS), inner)
        # 残留 LaTeX 结构（命令/花括号/脚本符）→ 放弃转换，保留原段
        if re.search(r"[\\{}^_]", inner):
            return m.group(0)
        return inner

    return _INLINE_MATH_RE.sub(_conv, text)


def _assign_heading_levels(blocks: list[ProcessedBlock]) -> None:
    """重建 heading 的 Markdown 层级。

    MinerU 的 text_level 对论文不可靠（几乎只输出 1/2）。
    真实的层级信号在标题的编号前缀里：
        "2"       → depth 1  (#)
        "3.1"     → depth 2  (##)
        "3.1.1"   → depth 3  (###)
        "(1)"     → depth = 最近点号深度 + 1
    无编号标题用位置/形状栈推断（固定段→#，摘要前→##，其余→#）。
    直接覆写 block.level。
    """
    headings = [b for b in blocks if b.kind == "heading"]
    if not headings:
        return

    # 先归一化空格拆字标题（"A B S T R A C T" → "Abstract"）与内联公式残留
    # （"Na $^{+}$ sites" → "Na⁺ sites"——heading 文本进 TOC/锚点，须为纯文本）
    for h in headings:
        cleaned = _heading_plain_math(_clean_spaced_heading(h.content))
        if cleaned != h.content:
            h.content = cleaned

    # 定位 Abstract/摘要，用于区分前置区与正文区
    abstract_idx = None
    for i, h in enumerate(headings):
        if re.sub(r"\s+", "", h.content).lower().rstrip(":.") in ("abstract", "摘要"):
            abstract_idx = i
            break

    last_dotted_depth = 0  # 最近一个点号编号的深度（供括号编号推断）
    roman_active = False   # 是否处于罗马数字编号体系（"I." → "A." → "1." 嵌套）
    seen_body_anchor = False  # 是否已过第一个正文锚点词（其后无编号非锚点标题 → 子节）

    for i, h in enumerate(headings):
        text = h.content
        text_lower = text.lower().rstrip(":.")
        # 空白归一化后的形式（兼容 "A B S T R A C T" 等拆字样式）
        norm = re.sub(r"\s+", "", text_lower)

        # 固定段（Abstract/摘要/References/Acknowledgments 等）→ 一级
        if text_lower in _FIXED_SECTIONS or norm in _FIXED_SECTIONS:
            h.level = 1
            # methods/experimental 类固定词同时是正文锚点（其后无编号标题 → 其子节）
            if re.sub(r"\s+", " ", text_lower).strip() in _BODY_ANCHOR_WORDS:
                seen_body_anchor = True
            continue

        # 罗马数字编号（APS 风格 "I. INTRODUCTION"）→ 一级，开启罗马体系上下文
        if _ROMAN_NUM_RE.match(text):
            h.level = 1
            roman_active = True
            continue

        # 罗马体系下的字母编号（"A. Motivation"）→ 二级
        if roman_active and _ALPHA_NUM_RE.match(text):
            h.level = 2
            continue

        # 点号编号前缀：深度 = 点分隔的组数；罗马体系下作为第三级起算
        m = _DOTTED_NUM_RE.match(text)
        if m:
            depth = len(m.group(1).split("."))
            h.level = min(depth + 2, 6) if roman_active else depth
            last_dotted_depth = h.level
            # 带编号的锚点词（"1. INTRODUCTION"）同样开启锚点上下文
            if re.sub(r"\s+", " ", m.group(2).lower().rstrip(":.")).strip() in _BODY_ANCHOR_WORDS:
                seen_body_anchor = True
            continue

        # 括号编号前缀：嵌套在最近点号层级之下
        m = _PAREN_NUM_RE.match(text)
        if m:
            h.level = (last_dotted_depth or 1) + 1
            continue

        # 无编号标题
        if i == 0:
            # 首个标题 = 论文标题
            h.level = 1
        elif abstract_idx is not None and i < abstract_idx:
            # 摘要前的前置区（HIGHLIGHTS / Affiliations 等）
            h.level = 2
        elif re.sub(r"\s+", " ", text_lower).strip() in _BODY_ANCHOR_WORDS:
            # 正文锚点词（Introduction/Methods/Results/Discussion...）→ 顶级
            h.level = 1
            seen_body_anchor = True
        elif seen_body_anchor:
            # 锚点之后的无编号非锚点标题 → 其子节（Nature 式 Methods 小节）
            h.level = 2
        elif last_dotted_depth > 0:
            # 编号章节之间的无编号标题 → 嵌在当前点号深度之下（书章子节等）
            h.level = min(last_dotted_depth + 1, 6)
        else:
            # 无锚点体系论文的无编号标题 → 顶级（由 _add_numbering 补编号）
            h.level = 1


# 形态 2 判定用的英语封闭类功能词（介词/连词/冠词/助动词/指示词等）：
# 前缀以这些词收尾 ⇒ 句子必未完结 ⇒ "Fig. N." 是句中引用而非图注起点
_INCOMPLETE_ENDING_WORDS = frozenset(
    "of and or but nor so yet for the a an in on at by with to from into onto upon over under "
    "about between among through during before after since until unless although though because "
    "if when while whereas that which who whom whose as than then thus hence is are was were be "
    "been being has have had do does did can could may might shall should will would must not no "
    "each every either neither both such its their his her our your my this these those there here "
    "it he she we they me him them us one all any some none most more less many much few several "
    "other another others same own very just only even also still via per vs versus etc".split()
)


def _split_figure_legends(blocks: list[ProcessedBlock]) -> list[ProcessedBlock]:
    """游离编号图注处理：把 "Figure N. ..." 文本块标为 fig_caption_text，
    供 _assign_figure_numbers 绑回未编号图组。

    两种形态：
    1. 整块独立图注（"Figure 7. Rapid rejection of ..."）
    2. 句中粘连（"...(Figure 7). These data Figure 6. <图注直到块尾>"）→ 切出图注部分；
       只处理图注延续到块尾的（块中间还有正文的无法可靠切分，放弃）
    """
    out = []
    prev_para = None  # (page_idx, content) 最近的正文段落（跨页断句守卫用）
    for b in blocks:
        if b.kind != "paragraph":
            out.append(b)
            continue
        t = b.content.strip()
        # 形态 1：独立图注块（"Figure 7." / "Fig. 1." 都算——Science 系用缩写，
        # 只认 Figure 会把游离图注漏掉，图组被后一个编号图注错吞）
        m = re.match(r"^(?:Figure|Fig\.?)\s+(\d+(?:\.\d+)*)[\.\:]\s+(\S.*)$", t, re.S)
        if m and len(m.group(2)) > 40:
            # 跨页断句守卫：页首块且上一段句未完结（不以 .!? 等收尾）时，
            # "Fig. N." 是上一句的句中引用而非图注起点（yang2021 实证：
            # "…photon energies of ‖跨页‖ Fig. 3. These data…" 幻影图注
            # 引发同号双写、整幅重裁被碎片覆盖）。保持段落不转换
            if (prev_para is not None and b.page_idx > prev_para[0]
                    and not re.search(r'[.!?…]["\'\)\]”’]*\s*$', prev_para[1])):
                out.append(b)
                prev_para = (b.page_idx, t)
                continue
            nb = ProcessedBlock("fig_caption_text", content=m.group(2).strip(),
                                page_idx=b.page_idx, src_page=getattr(b, "src_page", None))
            nb.fig_num = m.group(1)
            out.append(nb)
            continue
        # 形态 2：句中粘连，切分点是 "Figure N. "/"Fig. N. " 且其后直到块尾都是图注
        m2 = re.search(r"\b(?:Figure|Fig\.?)\s+(\d+(?:\.\d+)*)[\.\:]\s+([A-Z].{40,})$", t, re.S)
        if m2:
            prefix = t[: m2.start()].strip()
            # "Fig. N." 是否为句中引用：前缀尾词判定。尾词是英语封闭类功能词
            # （介词/连词/冠词/助动词/指示词等）则句子必未完结——引用而非图注起点
            # （yang2021 实证 "…photon energies of Fig. 3. These data…" 幻影图注致
            # 同号双写、整幅重裁被碎片覆盖）；尾词是内容词或 )/数字/罗马数字
            # （"(h) Fig. 1. …"、"(g) Region III Fig. 2. …" 面板标号收尾，park2021
            # 实证）则是图注起点，照切
            if prefix:
                last_word = re.search(r"([A-Za-z]+)$", prefix)
                is_reference = (
                    (last_word and last_word.group(1).lower() in _INCOMPLETE_ENDING_WORDS)
                    or re.search(r"(see|cf\.?|as shown|shown|如|见)$", prefix, re.I)
                )
            else:
                is_reference = False
            if prefix and not is_reference:
                if prefix:
                    out.append(ProcessedBlock("paragraph", content=prefix,
                                              page_idx=b.page_idx,
                                              src_page=getattr(b, "src_page", None)))
                nb = ProcessedBlock("fig_caption_text", content=m2.group(2).strip(),
                                    page_idx=b.page_idx,
                                    src_page=getattr(b, "src_page", None))
                nb.fig_num = m2.group(1)
                out.append(nb)
                continue
        out.append(b)
        prev_para = (b.page_idx, t)
    return out


# 粘连图注里的编号标记（"FIG. 5. " / "Figure 12.4: "）；标号后须接 "."/":"
# 加空白——"Fig. 3a"（面板字母）与 "Fig. 7)."（句中引用）都不命中
_GLUED_CAP_RE = re.compile(r"\b(?:Fig(?:ure)?\.?|FIG\.?)\s*(\d+(?:\.\d+)*)\s*[\.\:]\s+", re.I)


def _split_glued_caption(caption: str) -> list[tuple[str, str]]:
    """粘连多图注切分：caption 以编号标记起首且含 ≥2 个不同编号标记时，
    按标记切成 [(编号, 图注正文)]；任一条件不满足（首个标记不在开头、
    编号重复、某段过短不像真图注）→ 返回 []（不拆，保持原样）。"""
    caption = caption.strip()
    marks = list(_GLUED_CAP_RE.finditer(caption))
    if len(marks) < 2 or marks[0].start() != 0:
        return []
    nums = [m.group(1) for m in marks]
    if len(set(nums)) != len(nums):
        return []
    parts = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(caption)
        text = caption[m.end():end].strip()
        if len(text) < 40:
            return []
        parts.append((m.group(1), text))
    return parts


def _assign_figure_numbers(blocks: list[ProcessedBlock]) -> None:
    """图组编号：把子图块归并到所属 Figure，生成文件名与图注。

    MinerU 常把一个 Figure 拆成多个块（子图 a/b/c 各一块 + 带完整图注的一块）。
    规律：子图块（空 caption 或单字母）在前，带 "Fig. N" 完整图注的块在后。
    分组规则：页间隔 ≤1 的连续图片块为一组；带图编号的块结束一组并定错。
    输出：
        带图注的主块 → fig{N}.ext，图注 "Figure N: caption"
        子图块       → fig{N}{letter}.ext，图注 "Figure N (letter)"
        无编号图组   → figX{seq}.ext（如 graphical abstract）
    N 可能带小数（章节式编号 "12.4"），文件名中小数点转连字符（fig12-4.jpg）
    避免不同图编号截断为同一文件名互相覆盖。
    """
    images = [b for b in blocks if b.kind == "image"]
    if not images:
        return

    # 检测每个块的图编号与是否为主图注块（编号为字符串，如 "3" / "12.4"）
    def fig_num(b):
        m = _FIG_NUM_RE.search(b.caption or "")
        return m.group(1) if m else None

    def fig_stem(num: str) -> str:
        return "fig" + num.replace(".", "-")

    # 分组：按正文流顺序，页间隔 >1 或遇编号块时开新组。
    # 游离编号图注（fig_caption_text）也是组边界——图注编号直接定组号
    # （zhao2020 实证："Fig. 1." 游离图注在面板组之前，不参与归组会导致
    # 面板被下一个编号图组错吞）
    cap_blocks = [b for b in blocks if b.kind == "fig_caption_text"]
    cap_by_id = {id(b): b for b in cap_blocks}
    stream = [b for b in blocks
              if b.kind == "image" or b.kind == "fig_caption_text"]
    groups = []       # 每组: {"sub": [...], "main": block|None, "num": str|None, "cap": block|None}
    cur = {"sub": [], "main": None, "num": None, "cap": None}
    prev_page = None
    for b in stream:
        if b.kind == "fig_caption_text":
            n = getattr(b, "fig_num", None)
            if n is None:
                continue
            if cur["sub"] or cur["main"]:
                # caption-last：游离图注收尾当前组
                cur["num"] = n
                cur["cap"] = b
                groups.append(cur)
                cur = {"sub": [], "main": None, "num": None, "cap": None}
            else:
                # caption-first（zhao2020 'Fig. 1.' 实证）：图注先挂号开新组，
                # 等后续面板汇入
                if cur["cap"] is not None:
                    groups.append(cur)
                    cur = {"sub": [], "main": None, "num": None, "cap": None}
                cur["num"] = n
                cur["cap"] = b
            continue
        if prev_page is not None and b.page_idx - prev_page > 1:
            groups.append(cur)
            cur = {"sub": [], "main": None, "num": None, "cap": None}
        n = fig_num(b)
        if n is not None:
            # 编号主图块：当前组已被游离图注占号或已有主图时，先落盘再开新组
            if cur["cap"] is not None or cur["main"] is not None:
                groups.append(cur)
                cur = {"sub": [], "main": None, "num": None, "cap": None}
            cur["main"] = b
            cur["num"] = n
            groups.append(cur)
            cur = {"sub": [], "main": None, "num": None, "cap": None}
        else:
            cur["sub"].append(b)
        prev_page = b.page_idx
    if cur["sub"] or cur["main"] or cur["cap"]:
        groups.append(cur)

    # 为无编号组分配序号（figX 前缀与 fig{N} 不冲突，从 1 开始）
    extra_seq = 0

    # 文件名撞名防线：同一 Figure 编号被分成两组（幻影图注/SI 重号等）时，
    # 两组主图都会得名 fig{N}.jpg，渲染按文档序复制后者覆盖前者——静默丢图
    # （yang2021 实证：幻影 "Fig. 3." 组把整幅重裁覆盖成碎片）。撞名自动加 -2/-3
    used_names: set[str] = set()

    def _unique(name: str) -> str:
        if name not in used_names:
            used_names.add(name)
            return name
        stem, dot, ext = name.rpartition(".")
        k = 2
        while f"{stem}-{k}{dot}{ext}" in used_names:
            k += 1
        out = f"{stem}-{k}{dot}{ext}"
        used_names.add(out)
        return out

    letters = "abcdefghijklmnopqrstuvwxyz"
    for g in groups:
        num = g["num"]
        main = g["main"]
        subs = g["sub"]

        if num is None:
            # 无编号组（如 graphical abstract）
            extra_seq += 1
            for j, b in enumerate(subs):
                ext = Path(b.img_src).suffix if b.img_src else ".png"
                suffix = letters[j % 26] if len(subs) > 1 else ""
                b.img_new_name = f"figX{extra_seq}{suffix}{ext}"
                b.content = b.caption or f"Figure X{extra_seq}"
            continue

        # 游离图注定组（cap 有、main 无）：首图升为主图并携带图注，
        # 图注块标记移除（caption-first 版式，zhao2020 'Fig. 1.' 实证）
        if g["cap"] is not None and main is None and subs:
            main = subs.pop(0)
            g["main"] = main
            main.caption = f"Fig. {num}. {g['cap'].content}"
            g["cap"].kind = "_bound"

        # 多图注粘连拆分：主图图注内还含第二个 Fig./FIG. 编号标记（引擎把同页
        # 两张图的图注并到靠后的图块上），且组内图片数与图注段数恰一致 →
        # 按流序一一对应拆分（图注顺序与图片顺序一致。blanco2024 实测：
        # Fig.5/Fig.6 同页，双图注粘连于靠后图块，前置图被误当子图 fig5a）
        if main is not None and subs:
            parts = _split_glued_caption(main.caption or "")
            imgs = subs + [main]  # 流序：子图块在前，编号主图块收尾
            if parts and len(parts) == len(imgs) and parts[0][0] == num:
                for (n_i, cap_i), b in zip(parts, imgs):
                    ext = Path(b.img_src).suffix if b.img_src else ".png"
                    b.img_new_name = _unique(f"{fig_stem(n_i)}{ext}")
                    # caption 带 "Figure N:" 前缀（与正常主图块形态一致——
                    # figure_merger 的真图注组界判定靠它识别，裸图注会被
                    # 当无注碎片误并，blanco 实测）
                    b.caption = f"Figure {n_i}: {cap_i}" if cap_i else f"Figure {n_i}"
                    b.content = b.caption
                logger.info(
                    f"  图注粘连拆分: Figure {[p[0] for p in parts]}（{len(imgs)} 图）")
                continue

        # 子图块：字母后缀
        for j, b in enumerate(subs):
            ext = Path(b.img_src).suffix if b.img_src else ".png"
            # 优先用原单字母 caption，否则按顺序 a/b/c
            letter = b.caption.strip().lower() if (b.caption and len(b.caption.strip()) == 1 and b.caption.strip().isalpha()) else letters[j % 26]
            b.img_new_name = _unique(f"{fig_stem(num)}{letter}{ext}")
            b.content = f"Figure {num} ({letter})"

        # 主图注块
        if main is not None:
            ext = Path(main.img_src).suffix if main.img_src else ".png"
            main.img_new_name = _unique(f"{fig_stem(num)}{ext}")
            # 图注格式："Figure N: caption"（去掉 caption 里重复的 "Fig. N"/
            # "Figure N:" 前缀，N 可带小数；分隔符冒号句号都剥，防 "Figure N: : ..."）
            cap = re.sub(r"^Fig(?:ure|\.)?\s*\d+(?:\.\d+)*\s*[\.\:]?\s*", "", main.caption or "").strip()
            main.content = f"Figure {num}: {cap}" if cap else f"Figure {num}"
            # caption 同步为 "Figure N: ..." 前缀形态——figure_merger 的真图注
            # 组界判定（_REAL_CAPTION_RE 读 caption 优先）靠它认出主块，
            # 否则主块被当无注碎片、整组合并后图注随被丢块消失
            # （wang2024 实测：Fig.3/12/33/40 主块被丢、只剩 fig3a 子图）
            main.caption = main.content

    # --- 游离编号图注绑回（Blood/PNAS 式 "Figure N." 正文块 → 最近的未编号图组）---
    group_of = {}
    for g in groups:
        for sb in g["sub"]:
            group_of[id(sb)] = g
        if g["main"] is not None:
            group_of[id(g["main"])] = g
    used = {g["num"] for g in groups if g["num"] is not None}
    img_positions = [(idx, b) for idx, b in enumerate(blocks) if b.kind == "image"]

    for idx, b in enumerate(blocks):
        if b.kind != "fig_caption_text":
            continue
        n = getattr(b, "fig_num", None)
        # 只考察图注之前最近的一个图组（不跳过一个已编号组去够更远的组）
        target = None
        for ipos, ib in reversed(img_positions):
            if ipos >= idx:
                continue
            g = group_of.get(id(ib))
            if g is not None and g["num"] is None:
                target = g
            break
        if target is None:
            # 图注在图组之前（内容流里图注先行，Science 实证）：向后找最近的
            # 未编号组（同样不跳过已编号组）
            for ipos, ib in img_positions:
                if ipos <= idx:
                    continue
                g = group_of.get(id(ib))
                if g is not None and g["num"] is None:
                    target = g
                break
        if target is not None:
            gpage = max(x.page_idx for x in ([target["main"]] if target["main"] else []) + target["sub"])
            # 页距过大（图注离图组超过 1 页）不绑
            if b.page_idx - gpage > 1:
                target = None
        if target is None or n is None or n in used:
            # 绑不上：退回正文段落，并还原 "Figure N." 编号前缀
            b.kind = "paragraph"
            b.content = f"Figure {n}. {b.content}" if n else b.content
            continue
        # 组首图升为携带图注的主图
        imgs = ([target["main"]] if target["main"] else []) + target["sub"]
        first = imgs[0]
        ext = Path(first.img_src).suffix if first.img_src else ".png"
        first.img_new_name = f"fig{n}{ext}"
        first.content = f"Figure {n}: {b.content}" if b.content else f"Figure {n}"
        for j, sb in enumerate(imgs[1:]):
            ext = Path(sb.img_src).suffix if sb.img_src else ".png"
            letter = sb.caption.strip().lower() if (sb.caption and len(sb.caption.strip()) == 1 and sb.caption.strip().isalpha()) else letters[j % 26]
            sb.img_new_name = f"fig{n}{letter}{ext}"
            sb.content = f"Figure {n} ({letter})"
        target["num"] = n
        used.add(n)
        b.kind = "_bound"

    blocks[:] = [b for b in blocks if b.kind != "_bound"]


def _add_numbering(blocks: list[ProcessedBlock]) -> None:
    """为无编号体系的论文正文标题补编号（1, 2, 2.1 ...）。

    仅在论文整体无点号编号体系时生效；已有编号的论文保持原样。
    正文起点：Abstract 标题之后；无 Abstract 的论文（Nature/Wiley 风格，
    约占 2/3）则是论文标题之后第一个非固定段标题——否则会因永不触发
    而导致全部标题压平为 H1 且无编号。
    """
    headings = [b for b in blocks if b.kind == "heading"]
    if not headings:
        return

    # 如果已有编号体系，不额外加编号
    if _has_existing_numbers(headings):
        return

    has_abstract = any(
        re.sub(r"\s+", "", h.content).lower().rstrip(":.") in ("abstract", "摘要")
        for h in headings
    )

    counters = {}  # level -> counter
    body_started = False
    seen_title = False  # 首个标题视为论文标题（无 Abstract 时用于定位正文起点）

    for h in headings:
        text_lower = h.content.lower().rstrip(":.")
        norm = re.sub(r"\s+", "", text_lower)

        if norm in ("abstract", "摘要"):
            body_started = True
            continue
        # 固定段不编号
        if text_lower in _FIXED_SECTIONS or norm in _FIXED_SECTIONS:
            continue
        if not body_started:
            if has_abstract:
                # 摘要前的前置区不编号
                continue
            if not seen_title:
                # 论文标题
                seen_title = True
                continue
            # 无 Abstract：标题之后第一个非固定段标题即正文起点
            body_started = True
        # 正文锚点词（Introduction/Methods/Results...）保持无编号（Nature 风格），
        # 同时视为正文起点
        if re.sub(r"\s+", " ", text_lower).strip() in _BODY_ANCHOR_WORDS:
            body_started = True
            continue
        # 子节（## 及以下）不编号：它们多是锚点章节的小节（Methods 小节等）
        if h.level >= 2:
            continue
        # 罗马/字母编号体系已有编号，原样保留
        if _ROMAN_NUM_RE.match(h.content) or _ALPHA_NUM_RE.match(h.content):
            continue
        # 已有编号前缀的不重复加
        if _DOTTED_NUM_RE.match(h.content) or _PAREN_NUM_RE.match(h.content):
            _sync_counters(h.content, counters)
            continue

        level = h.level
        number = _next_heading_number(level, counters)
        h.content = f"{number} {h.content}"


def _merge_paragraph_fragments(blocks: list[ProcessedBlock]) -> list[ProcessedBlock]:
    """合并被拆开的段落碎片（MinerU 常把一段拆成多块，或跨页断开）。

    合并闸门（规则框架借鉴 MinerU-Popo 的 merge_rules/is_list_item）：
    前块不以终止符结尾且较短；终止符含 CJK 与"终止符+闭引号/括号"组合；
    后块是列表项开头、或两块都以数字开头 → 不合并；
    允许跨页合并，但要求续段信号（后块小写/开括号起首，或前块逗号/连字符结尾）；
    作者署名/机构块（含 $^{ 上标或 By 开头）不与相邻段落合并。
    """
    if not blocks:
        return blocks

    def _is_byline(t: str) -> bool:
        ts = t.strip()
        # 署名行（多个上标，如 "X $^{1}$, Y $^{2}$"）、机构行（$^{ 开头）、"By X" 开头；
        # 短文本限定——含大量上标的科学长段落不是署名
        if len(ts) >= 300:
            return False
        return (ts.count("$^{") >= 2 or ts.startswith("$^{")
                or re.match(r"^By\s+", ts, re.IGNORECASE) is not None)

    def _ends_terminal(t: str) -> bool:
        t = t.rstrip()
        if not t or t.endswith("$$"):
            return True
        if t[-1] in ".。?!？！:：;；…":
            return True
        # 终止符 + 闭引号/括号（"...said." / （…完成。）
        return len(t) >= 2 and t[-1] in "”’\"')）】」》" and t[-2] in ".。?!？！:：;；…"

    _LIST_ITEM_RE = re.compile(
        r"^\s*(?:\d{1,2}[.)]\s|[（(]\d{1,2}[）)]|[（(][a-zA-Z][）)]|[•▪▫●○◦]|\\?-\s"
        r"|[①-⑳]|第[0-9一二三四五六七八九十百]+[条节章节]|[A-Z]\.\s|[IVXLC]{1,4}\.\s)")

    def _cross_page_ok(prev_t: str, next_t: str) -> bool:
        """跨页续段信号：后块小写/开括号起首，或前块逗号/连字符/虚词结尾"""
        if re.match(r"^[a-z(]", next_t):
            return True
        if prev_t.rstrip().endswith((",", "，", "、", "-", "–", "—")):
            return True
        # 前块以英语虚词结尾（the/of/to/and...）——完整段落不会这样收尾
        return bool(re.search(
            r"\b(the|a|an|of|to|and|or|in|on|at|with|by|from|as|is|are|was|were|be|been|that|which|for|not|no)$",
            prev_t.rstrip(), re.I))

    result: list[ProcessedBlock] = []
    last_para_idx: int | None = None
    gap_block = False  # 最后段落与当前块之间隔着图片/表格（Popo 式跨块配对）
    for block in blocks:
        if block.kind == "paragraph":
            if last_para_idx is not None:
                prev = result[last_para_idx]
                same_page = block.page_idx == prev.page_idx
                cross_page = block.page_idx == prev.page_idx + 1
                # 跨页、或中间隔了图/表（句子被插图打断）：需续段信号；
                # 有信号背书时长度门槛放宽（链式合并会让前块持续增长）
                need_signal = gap_block or cross_page
                if (
                    ((same_page and not need_signal) or (need_signal and _cross_page_ok(prev.content, block.content)))
                    and len(prev.content) < (4000 if need_signal else 200)
                    and not _ends_terminal(prev.content)
                    and not _LIST_ITEM_RE.match(block.content)
                    and not (prev.content[:1].isdigit() and block.content[:1].isdigit())
                    and not _is_byline(prev.content)   # 署名块不并入下文
                    and not _is_byline(block.content)  # 也不把署名块并入上文
                ):
                    # 合并：用空格连接
                    prev.content = prev.content.rstrip() + " " + block.content.lstrip()
                    gap_block = False
                    continue
            result.append(block)
            last_para_idx = len(result) - 1
            gap_block = False
            continue
        result.append(block)
        if block.kind in ("heading", "equation", "reference", "footnote"):
            last_para_idx = None
            gap_block = False
        elif block.kind in ("image", "table", "table_image") and last_para_idx is not None:
            gap_block = True

    return result


def _next_heading_number(level: int, counters: dict) -> str:
    """生成下一个 heading 编号，如 1, 2, 2.1, 2.1.1"""
    # 递增当前层级计数
    counters[level] = counters.get(level, 0) + 1
    # 重置所有更深层级
    for k in list(counters.keys()):
        if k > level:
            del counters[k]

    # 构建编号字符串
    parts = []
    for lv in sorted(counters.keys()):
        if lv <= level:
            parts.append(str(counters[lv]))
    return ".".join(parts)


def _sync_counters(text: str, counters: dict):
    """从已有编号的标题同步计数器状态"""
    m = re.match(r"^(\d+(?:\.\d+)*)\.?\s+", text)
    if not m:
        return
    nums = [int(x) for x in m.group(1).split(".")]
    for i, n in enumerate(nums, 1):
        counters[i] = n
    # 清除更深层级
    for k in list(counters.keys()):
        if k > len(nums):
            del counters[k]


def _has_existing_numbers(headings: list[ProcessedBlock]) -> bool:
    """检查 heading 中是否已有编号体系（点号数字或罗马数字）"""
    if not headings:
        return False
    numbered = sum(1 for h in headings if _DOTTED_NUM_RE.match(h.content))
    if numbered >= max(2, len(headings) * 0.2):
        return True
    # 罗马数字章节（APS 风格 "I./II."）：≥2 个即视为已有编号体系
    roman = sum(1 for h in headings if _ROMAN_NUM_RE.match(h.content))
    return roman >= 2


def _has_number_prefix(text: str) -> bool:
    """检查标题是否有数字编号前缀（如 "1 ", "2.1 ", "3.2.1 "，含罗马/字母编号）"""
    return bool(re.match(r"^\d+(\.\d+)*\.?\s+", text)
                or _ROMAN_NUM_RE.match(text) or _ALPHA_NUM_RE.match(text))
