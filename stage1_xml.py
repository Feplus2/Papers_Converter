# -*- coding: utf-8 -*-
"""Stage 1（XML 输入）：JATS / Elsevier 变体 XML → content_list 适配器。

定位：与 stage1_mineru/paddleocr 同级的「解析器」，但解析是本地确定性的
（XML 自带语义结构，无 OCR/无 VLM/无重试闸）。产物与 PDF 引擎 staging 完全
同构——{stem}_content_list.json + images/——下游 convert_single 全复用，
阅读器/向量化/翻译不感知来源（paper-format-contract.md 契约不变）。

产出三件套（staging 目录）：
  {stem}_content_list.json   块序列（块 schema 与 MinerU 对齐：text/image/
                             equation/table/list，img_path 相对 images/）
  images/                    本地可解析的图片/公式图（href 同目录或子目录拷入）
  xml_meta.json              XML front matter 权威元数据（走 zotero_meta 通道
                             注入 extract_metadata——author/date/container-title
                             以它为准，同 PDF 路径的 Zotero 权威语义）
  xml_references.json        <ref-list> 结构化参考文献 payload（references.json
                             同 schema，source="xml"；比 PDF 路径正则+LLM 重建可靠）

方言覆盖：
  - JATS（PMC/eLife/Hindawi 等）：front/article-meta + body/sec + back/ref-list；
    公式 tex-math 优先，mml:math 走 mathml_tex，graphic 形态公式降级占位
  - Elsevier ce: 变体（ScienceDirect 全文）：iteminfo/doi + ce:sections/section/
    section-title/para + ce:formula（mml:math）+ ce:figure + ce:table + ce:bib
    （fixture 按 ce 公开 DTD 结构构造；真实样本联调待 ELSEVIER_API_KEY，
     Phase 1.5 后如有偏差在此适配）
"""

import html.entities
import json
import logging
import re
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path

logger = logging.getLogger(__name__)

# 命名实体守卫：JATS/Elsevier 存档偶含 HTML 实体（&nbsp; 等），ET 只认 XML 五件套
_SAFE_ENTITIES = {"amp", "lt", "gt", "quot", "apos"}
_ENT_RE = re.compile(r"&([a-zA-Z][a-zA-Z0-9]*);")

_SKIP_TEXT_TAGS = {  # 行内不进正文的结构性标签
    "xref",  # 交叉引用取文本（另行特判 bibr 补 []）
}

_XLINK_HREF = "{http://www.w3.org/1999/xlink}href"


def _href_of(el: ET.Element) -> str:
    """graphic/link 的 href 属性：本地名剥了 ns，属性键还是限定名，两者都认。"""
    return el.get("href") or el.get(_XLINK_HREF) or ""


def _localname(tag) -> str:
    if not isinstance(tag, str):
        return ""
    return tag.split("}")[-1]


def _strip_ns(el: ET.Element) -> ET.Element:
    el.tag = _localname(el.tag)
    for child in el:
        _strip_ns(child)
    return el


def _guard_entities(text: str) -> str:
    def sub(m):
        name = m.group(1)
        if name in _SAFE_ENTITIES:
            return m.group(0)
        v = html.entities.html5.get(name) or html.entities.html5.get(name + ";")
        if v:
            return v
        cp = html.entities.name2codepoint.get(name)
        return chr(cp) if cp else name
    return _ENT_RE.sub(sub, text)


def load_xml(xml_path: Path) -> ET.Element:
    raw = xml_path.read_bytes()
    # 编码嗅探交给 ET（声明优先）；实体守卫在文本层做（utf-8 假设覆盖绝大多数学术存档，
    # 非 utf-8 时回退 latin-1 容错读取——实体守卫只动 ASCII 片段，安全）
    for enc in ("utf-8", "latin-1"):
        try:
            return _strip_ns(ET.fromstring(_guard_entities(raw.decode(enc))))
        except (ET.ParseError, UnicodeDecodeError):
            continue
    raise ValueError(f"XML 解析失败（非 well-formed）: {xml_path}")


def detect_flavor(root: ET.Element) -> str:
    """'jats' | 'elsevier'。按结构标记判定（localname 层，命名空间差异免疫）。"""
    names = {_localname(e.tag) for e in root.iter()}
    if "article-meta" in names or "ref-list" in names:
        return "jats"
    if "iteminfo" in names or ("sections" in names and "section-title" in names):
        return "elsevier"
    if _localname(root.tag) == "article":
        return "jats"
    return "jats"


# ============================================================
# 行内文本：元素 → Markdown 行内片段（公式/上下标/引用）
# ============================================================

def _find_first(el: ET.Element, *names: str) -> ET.Element | None:
    want = set(names)
    for e in el.iter():
        if e is not el and _localname(e.tag) in want:
            return e
    return None


def _formula_latex(container: ET.Element) -> str | None:
    """公式元素内容 → LaTeX：tex-math 优先 > mml:math > None（graphic 形态由调用方处理）。"""
    for e in container.iter():
        if _localname(e.tag) == "tex-math":
            t = "".join(e.itertext()).strip()
            if t:
                return t
    for e in container.iter():
        if _localname(e.tag) == "math":
            from mathml_tex import mathml_to_latex
            return mathml_to_latex(e)
    return None


def _inline_any(el: ET.Element, base_dir: Path, images: dict) -> str:
    """单个元素的行内转换：先按自身标签分派（xref/sub/sup/公式等），
    再退回整树拼接。paragraph() 按子元素逐个调用时，特殊分支不丢。"""
    from mathml_tex import mathml_to_latex
    tag = _localname(el.tag)
    if tag == "inline-formula":
        latex = _formula_latex(el)
        return f"${latex}$" if latex else ""
    if tag == "formula" and el.get("type") == "inline":  # Elsevier 行内公式
        latex = _formula_latex(el)
        return f"${latex}$" if latex else ""
    if tag == "math":
        return f"${mathml_to_latex(el)}$"
    if tag == "sub" or tag == "inf":  # inf = Elsevier ce:inf 下标
        inner = _inline_text(el, base_dir, images).strip()
        return f"<sub>{inner}</sub>" if inner else ""
    if tag == "sup":
        inner = _inline_text(el, base_dir, images).strip()
        return f"<sup>{inner}</sup>" if inner else ""
    if tag == "xref":
        t = _inline_text(el, base_dir, images).strip()
        rt = el.get("ref-type")
        if rt == "bibr":
            if re.fullmatch(r"\d+[\w,;\s–—-]*", t or ""):
                t = f"[{t}]"
            # 引文转跳重建（PDF 路径 link_extractor 同款格式）：[N] → [N](#ref-N)，
            # 多值逐个链接（"[2]–[3]" → [2](#ref-2)–[3](#ref-3)）；锚点由 refs 条目号发射
            t = re.sub(r"\[(\d+)([a-z]?)\]", lambda m: f"[{m.group(1)}{m.group(2)}](#ref-{m.group(1)})", t)
            return t
        if rt == "fig" or rt == "table" or rt == "scheme":
            # 图/表转跳重建：编号取 rid 数字（SN rid 形如 Fig3）或文本本身；
            # 锚点 fig-N/tab-N 由图注编号发射（renderer 按锚点集发射）
            m = re.search(r"(\d+)", el.get("rid") or "") or re.search(r"(\d+)", t or "")
            if m:
                kind = "tab" if rt == "table" else "fig"
                return f"[{t}](#{kind}-{m.group(1)})"
        return t
    if tag == "break":
        return " "
    if tag in ("inline-graphic", "graphic"):
        href = _href_of(el)
        local = _resolve_graphic(href, base_dir)
        if local:
            name = _register_image(local, images)
            return f"![{name}](images/{name})"
        return f"[图: {href}]"
    return _inline_text(el, base_dir, images)


def _inline_text(el: ET.Element, base_dir: Path, images: dict) -> str:
    """元素 → 行内文本：子元素递归，公式插 $...$，sub/sup 上下标，bibr 引文补 []。"""
    parts: list[str] = []
    if el.text:
        parts.append(el.text)
    for child in el:
        parts.append(_inline_any(child, base_dir, images))
        if child.tail:
            parts.append(child.tail)
    return "".join(parts)


def _normalize_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


# ============================================================
# 图片解析（href → staging images/ 拷入）
# ============================================================

def _resolve_graphic(href: str, base_dir: Path) -> Path | None:
    if not href or href.startswith(("http://", "https://")):
        return None
    for cand in (base_dir / href, base_dir.parent / href,
                 base_dir / "media" / href, base_dir / "graphics" / href):
        if cand.exists() and cand.is_file():
            return cand
    return None


def _register_image(src: Path, images: dict) -> str:
    """staging 命名防碰撞：同名不同目录时加序号。"""
    name = src.name
    if name in images and images[name] != src:
        stem, suffix = src.stem, src.suffix
        i = 1
        while f"{stem}_{i}{suffix}" in images:
            i += 1
        name = f"{stem}_{i}{suffix}"
    images[name] = src
    return name


# ============================================================
# JATS front matter → 权威元数据（CSL 对齐）
# ============================================================

def _jats_meta(root: ET.Element) -> dict:
    meta: dict = {}
    front = _find_first(root, "front")
    art_meta = _find_first(root, "article-meta") if front is not None else None
    target = art_meta if art_meta is not None else (front or root)

    def _first_text(scope: ET.Element, *path: str) -> str:
        el = scope
        for name in path:
            el = next((c for c in el if _localname(c.tag) == name), None)
            if el is None:
                return ""
        return _normalize_ws("".join(el.itertext()))

    title_el = _find_first(target, "article-title")
    if title_el is None:  # Element 假值陷阱：无子节点的 Element 是 falsy，禁用 or 链
        title_el = _find_first(root, "article-title")
    if title_el is not None:
        # 标题里的行内公式降级为纯文本（frontmatter YAML 数学语法无渲染通道）
        meta["title"] = _normalize_ws("".join(title_el.itertext()))

    # DOI / 期刊
    if art_meta is not None:
        for aid in art_meta:
            if _localname(aid.tag) == "article-id" and aid.get("pub-id-type") == "doi":
                meta["doi"] = _normalize_ws(aid.text or "")
                break
    if front is not None:
        meta["container-title"] = _first_text(front, "journal-meta", "journal-title-group", "journal-title")

    # 作者（contrib-type=author；string-name/name 拼接，collab 机构作者整名）
    authors = []
    if art_meta is not None:
        for contrib in art_meta.iter("contrib"):
            if contrib.get("contrib-type") not in (None, "author"):
                continue
            name = next((c for c in contrib if _localname(c.tag) in ("string-name", "name")), None)
            collab = next((c for c in contrib if _localname(c.tag) == "collab"), None)
            if collab is not None:
                authors.append({"name": _normalize_ws("".join(collab.itertext()))})
            elif name is not None:
                given = _normalize_ws(_first_text(name, "given-names"))
                surname = _normalize_ws(_first_text(name, "surname"))
                full = (given + " " + surname).strip() or _normalize_ws("".join(name.itertext()))
                if full:
                    authors.append({"name": full})
        # 机构（取首个 aff 作第一作者 affiliation 近似——frontmatter 只展示用）
        aff = next((a for a in art_meta if _localname(a.tag) == "aff"), None)
        if aff is not None and authors:
            authors[0].setdefault("affiliation", _normalize_ws("".join(aff.itertext()))[:120])
    if authors:
        meta["author"] = authors

    # 日期：pub 优先（pub-type/date-type），year 必有
    date_str = ""
    if art_meta is not None:
        for pd in art_meta:
            if _localname(pd.tag) != "pub-date":
                continue
            if pd.get("pub-type") in ("pub", None) or pd.get("date-type") in ("pub", None):
                y = _first_text(pd, "year")
                m = _first_text(pd, "month")
                d = _first_text(pd, "day")
                if y:
                    date_str = "-".join(x for x in (y, m, d) if x)
                    break
        if not date_str:
            y = _first_text(art_meta, "pub-date", "year")
            if y:
                date_str = y
    if date_str:
        meta["date"] = date_str

    # 摘要（含 <p> 全文；跳过图形摘要 specific-use）
    abstract_el = None
    for e in (art_meta or root).iter("abstract"):
        if e.get("abstract-type") == "graphical" or e.get("specific-use"):
            continue
        abstract_el = e
        break
    if abstract_el is not None:
        paras = []
        for p in abstract_el.iter("p"):
            paras.append(_normalize_ws("".join(p.itertext())))
        text = " ".join(x for x in paras if x).strip()
        if not text:
            text = _normalize_ws("".join(abstract_el.itertext()))
        if text:
            meta["abstract"] = text

    # 卷期页 / 关键词
    if art_meta is not None:
        for key, name in (("volume", "volume"), ("issue", "issue")):
            v = _first_text(art_meta, name)
            if v:
                meta[key] = v
        page = _first_text(art_meta, "fpage")
        lpage = _first_text(art_meta, "lpage")
        if not page:
            page = _first_text(art_meta, "elocation-id")
        if page:
            meta["page"] = f"{page}-{lpage}" if lpage else page
        kws = [_normalize_ws("".join(k.itertext()))
               for k in art_meta.iter("kwd")]
        kws = [k for k in kws if k]
        if kws:
            meta["keywords"] = kws[:15]
    return meta


def _elsevier_meta(root: ET.Element) -> dict:
    meta: dict = {}
    # 标题：article 级 ce:title（section 标题用 section-title，不同名）
    for e in root.iter("title"):
        t = _normalize_ws("".join(e.itertext()))
        if t:
            meta["title"] = t
            break
    doi = next((e for e in root.iter("doi")), None)
    if doi is not None:
        meta["doi"] = _normalize_ws("".join(doi.itertext()))
    # 期刊（ce:journal-title / publicationName）
    for name in ("journal-title", "publicationName"):
        e = next((x for x in root.iter(name)), None)
        if e is not None:
            meta["container-title"] = _normalize_ws("".join(e.itertext()))
            break
    # 作者：ce:author-group 内 given-name + surname
    authors = []
    for ag in root.iter("author-group"):
        for au in ag:
            if _localname(au.tag) != "author":
                continue
            given = next((c for c in au if _localname(c.tag) == "given-name"), None)
            surname = next((c for c in au if _localname(c.tag) == "surname"), None)
            full = " ".join(x for x in (
                _normalize_ws("".join(given.itertext())) if given is not None else "",
                _normalize_ws("".join(surname.itertext())) if surname is not None else "") if x)
            if full:
                authors.append({"name": full})
    if authors:
        meta["author"] = authors
    # 日期：coverDate / pubdate / date / published 取年份或 ISO
    for name in ("coverDate", "cover-date", "pubdate", "pub-date", "date", "published"):
        e = next((x for x in root.iter(name)), None)
        if e is not None:
            t = _normalize_ws("".join(e.itertext()))
            if re.search(r"\d{4}", t):
                meta["date"] = t
                break
    ab = next((x for x in root.iter("abstract")), None)
    if ab is not None:
        paras = [_normalize_ws("".join(p.itertext())) for p in ab.iter("para")]
        text = " ".join(x for x in paras if x) or _normalize_ws("".join(ab.itertext()))
        if text:
            meta["abstract"] = text
    return meta


# ============================================================
# 正文游走 → content_list
# ============================================================

def _caption_text(el: ET.Element | None) -> str:
    if el is None:
        return ""
    paras = [_normalize_ws("".join(p.itertext())) for p in el.iter("p")]
    paras = [p for p in paras if p]
    if paras:
        return " ".join(paras)
    return _normalize_ws("".join(el.itertext()))


def _table_html(table_el: ET.Element) -> str:
    """XHTML 表 → HTML 字符串（localname 序列化；colspan/rowspan 原样保留）。"""
    el = table_el
    el = _strip_ns(el)
    html = ET.tostring(el, encoding="unicode")
    # ET 输出自闭合与实体形态可被 CommonMark/浏览器接受，直接透传
    return html.strip()


def _fetch_sn_media(href: str, doi: str, images_dir: Path) -> Path | None:
    """Springer Nature 远端媒体下载：MediaObjects/{file} → media.springernature.com 全图。

    SN JATS 的 graphic href 是 MediaObjects 相对名（非本地文件），全文图在出版社
    内容服务器上；URL 由文章 DOI 规则构造（2026-08-27 实测 200，3.35MB PNG）：
    https://media.springernature.com/full/springer-static/image/art%3A{doi 的 / 转 %2F}/MediaObjects/{file}
    失败（网络/404/非图）返回 None 走既定降级路径（图注文本保底）。
    """
    if not doi or "MediaObjects/" not in href:
        return None
    import requests
    filename = href.rsplit("MediaObjects/", 1)[-1]
    # 路径穿越防御：XML href 不可信，剥掉目录段只留文件名（防 "../" 段越出 images/ 落盘）
    filename = Path(filename).name
    if not filename:
        return None
    doi_enc = doi.replace("/", "%2F")
    url = f"https://media.springernature.com/full/springer-static/image/art%3A{doi_enc}/MediaObjects/{filename}"
    try:
        resp = requests.get(url, timeout=30)
        if resp.status_code != 200 or len(resp.content) < 1000:
            return None
        ctype = resp.headers.get("content-type", "")
        if ctype and "image" not in ctype:
            return None
        images_dir.mkdir(parents=True, exist_ok=True)
        out = images_dir / filename
        out.write_bytes(resp.content)
        logger.info(f"  [springer-media] 下载 {filename} ({len(resp.content)} bytes)")
        return out
    except Exception as e:
        logger.warning(f"  [springer-media] 下载失败 {filename}: {e}")
        return None


class _Walker:
    def __init__(self, base_dir: Path, doi: str = "", media_dir: Path | None = None):
        self.base_dir = base_dir
        self.doi = doi
        self.media_dir = media_dir  # staging images/（SN 远端媒体落点）
        self.images: dict[str, Path] = {}
        self.blocks: list[dict] = []
        self.degraded_figures = 0
        self.degraded_formulas = 0
        self.remote_images = 0

    def heading(self, text: str, level: int):
        if text:
            self.blocks.append({"type": "text", "text_level": min(level, 3),
                                "text": text, "page_idx": 0})

    def para(self, text: str):
        if text:
            self.blocks.append({"type": "text", "text": text, "page_idx": 0})

    def formula(self, df_el: ET.Element):
        """disp-formula / ce:formula → 公式块。tex-math > MathML > 本地图 > 占位文本。"""
        label = next(df_el.iter("label"), None)
        label_t = _normalize_ws("".join(label.itertext())) if label is not None else ""
        latex = _formula_latex(df_el)
        if latex:
            self.equation(latex, label_t)
            return
        g = next(df_el.iter("graphic"), None)
        if g is not None:
            href = _href_of(g)
            local = _resolve_graphic(href, self.base_dir)
            if local is not None:
                name = _register_image(local, self.images)
                self.blocks.append({"type": "image", "img_path": f"images/{name}",
                                    "image_caption": [f"Equation {label_t}".strip()],
                                    "page_idx": 0})
                return
        self.degraded_formulas += 1
        self.para(f"[公式 {label_t}] {href if g is not None else ''}".strip())

    def paragraph(self, p_el: ET.Element):
        """JATS 段落：段内 disp-formula（JATS 合法嵌套）拆为独立公式块，其余行内拼接。"""
        buf: list[str] = []
        if p_el.text:
            buf.append(p_el.text)

        def flush():
            # buf 各项自带边界空白（text/tail），直接拼接（空格 join 会在
            # "[1]" 与 "." 之间插空格、拆散 j_0 之类的行内结构）
            text = _normalize_ws("".join(buf))
            if text:
                self.blocks.append({"type": "text", "text": text, "page_idx": 0})
            buf.clear()

        for child in p_el:
            tag2 = _localname(child.tag)
            # 行内 formula（ce:formula type="inline"）留在文本流；display 级才拆块
            if tag2 in ("disp-formula", "formula") and not (
                    tag2 == "formula" and child.get("type") == "inline"):
                flush()
                self.formula(child)
            elif tag2 == "fig":
                # SN 常把 fig 嵌在段落里：拆块走 figure 通道（行内通道只吐占位文本）
                flush()
                self.fig_element(child)
            elif tag2 == "table-wrap":
                flush()
                self.table_wrap_element(child)
            else:
                buf.append(_inline_any(child, self.base_dir, self.images))
            if child.tail:
                buf.append(child.tail)
        flush()

    def equation(self, latex: str, label: str = ""):
        tex = latex.strip()
        if label and "\\tag" not in tex:
            tex = f"{tex} \\tag{{{label}}}"
        self.blocks.append({"type": "equation", "text": tex, "page_idx": 0})

    def footnote(self, fn_el: ET.Element):
        """JATS <fn>（author-notes 通讯邮箱 / fn-group 脚注）→ page_footnote 块。

        复用 PDF 路径的脚注通道（content_processor 解析编号 / renderer 出 [^N]: 定义），
        数字 label 走编号脚注，符号 label（* † 等）按 PDF 路径符号脚注口径原样成段。
        """
        label = next(fn_el.iter("label"), None)
        label_t = _normalize_ws("".join(label.itertext())) if label is not None else ""
        paras = [_normalize_ws("".join(p.itertext())) for p in fn_el.iter("p")]
        text = " ".join(x for x in ([label_t] if label_t else []) + [t for t in paras if t])
        if text:
            self.blocks.append({"type": "page_footnote", "text": text, "page_idx": 0})

    def fig_element(self, fig_el: ET.Element):
        """JATS <fig>（sec 级或段落内嵌两路入口）：label+caption → figure()。"""
        cap = _caption_text(next(fig_el.iter("caption"), None))
        label = next(fig_el.iter("label"), None)
        label_t = _normalize_ws("".join(label.itertext())) if label is not None else ""
        cap_full = f"{label_t} {cap}".strip() if label_t else cap
        # Europe PMC 每图带 jpg+gif 双 graphic（同图双格式），只取首个
        g = next(fig_el.iter("graphic"), None)
        if g is not None:
            self.figure(_href_of(g), cap_full)
        elif cap_full:
            self.para(cap_full)

    def table_wrap_element(self, wrap_el: ET.Element):
        """JATS <table-wrap>：caption + HTML 表 / graphic 表。"""
        cap = _caption_text(next(wrap_el.iter("caption"), None))
        label = next(wrap_el.iter("label"), None)
        label_t = _normalize_ws("".join(label.itertext())) if label is not None else ""
        cap_full = f"{label_t} {cap}".strip() if label_t else cap
        tbl = next(wrap_el.iter("table"), None)
        if tbl is not None:
            self.table(tbl, cap_full)
        else:
            g = next(wrap_el.iter("graphic"), None)
            if g is not None:
                self.figure(_href_of(g), cap_full)

    def figure(self, href: str, caption: str, kind: str = "image"):
        local = _resolve_graphic(href, self.base_dir)
        if local is None and self.doi and self.media_dir is not None and "MediaObjects/" in href:
            remote = _fetch_sn_media(href, self.doi, self.media_dir)
            if remote is not None:
                self.remote_images += 1
                local = remote
        if local is None:
            # 图不可得：图注文本降级为段落（零文本丢失），计退化数
            self.degraded_figures += 1
            if caption:
                self.para(caption)
            return
        name = _register_image(local, self.images)
        blk = {"type": "chart" if kind == "chart" else "image",
               "img_path": f"images/{name}", "page_idx": 0}
        if caption:
            blk["image_caption"] = [caption]
        self.blocks.append(blk)

    def table(self, table_el: ET.Element, caption: str):
        body = _table_html(table_el)
        if not body:
            return
        blk = {"type": "table", "table_body": body, "page_idx": 0}
        if caption:
            blk["table_caption"] = [caption]
        self.blocks.append(blk)


def _walk_jats_sec(sec: ET.Element, w: _Walker, depth: int):
    for child in sec:
        tag = _localname(child.tag)
        if tag == "title" or tag == "section-title":
            w.heading(_normalize_ws("".join(child.itertext())), depth)
        elif tag == "sec":
            _walk_jats_sec(child, w, depth + 1)
        elif tag == "p":
            w.paragraph(child)
        elif tag == "disp-formula":
            w.formula(child)
        elif tag == "fig":
            w.fig_element(child)
        elif tag == "table-wrap":
            w.table_wrap_element(child)
        elif tag == "list":
            for li in child:
                if _localname(li.tag) == "list-item":
                    w.para(_normalize_ws(_inline_text(li, w.base_dir, w.images)))
        elif tag == "disp-quote":
            for p in child:
                if _localname(p.tag) == "p":
                    w.paragraph(p)
        elif tag == "boxed-text":
            for p in child.iter("p"):
                w.paragraph(p)
        # app（附录）按 sec 走；fn/notes 等零散结构忽略（正文主体不受影响）


def _walk_elsevier(root: ET.Element, w: _Walker):
    sections = next((e for e in root.iter("sections")), None)
    if sections is None:
        return

    def walk_sec(sec: ET.Element, depth: int):
        for child in sec:
            tag = _localname(child.tag)
            if tag == "section":
                walk_sec(child, depth + 1)
            elif tag == "section-title":
                w.heading(_normalize_ws("".join(child.itertext())), depth)
            elif tag == "para":
                w.paragraph(child)
            elif tag == "formula":
                label_el = next((c for c in child if _localname(c.tag) == "label"), None)
                label = _normalize_ws("".join(label_el.itertext())) if label_el is not None else ""
                latex = _formula_latex(child)
                if latex:
                    w.equation(latex, label)
                else:
                    w.degraded_formulas += 1
                    w.para(f"[公式 {label}]".strip())
            elif tag == "figure":
                cap = _caption_text(next((c for c in child if _localname(c.tag) == "caption"), None))
                label = next((c for c in child if _localname(c.tag) == "label"), None)
                label_t = _normalize_ws("".join(label.itertext())) if label is not None else ""
                cap_full = f"{label_t} {cap}".strip() if label_t else cap
                hrefs = [_href_of(e) for e in child.iter("link")] + \
                        [_href_of(e) for e in child.iter("inter-ref")] + \
                        [_href_of(e) for e in child.iter("graphic")]
                for href in hrefs:
                    if href:
                        w.figure(href, cap_full)
            elif tag == "table":
                cap = _caption_text(next((c for c in child if _localname(c.tag) == "caption"), None))
                w.table(child, cap)

    walk_sec(sections, 1)


# ============================================================
# 参考文献结构化提取（<ref-list> / ce:bibliography → references payload）
# ============================================================

def _citation_raw(elem_cit: ET.Element) -> str:
    """element-citation → 可读 raw（无 mixed-citation 时的兜底）。

    直接子元素各成一节以空格相连（itertext 整树拍平会把
    "Smith"+"J" 粘成 "SmithJ"）；person-group 内 name 先拼 "Given Surname"。"""
    parts = []
    # 元素自身的直接文本（纯文本型 mixed-citation 的全部内容都在 .text，无子元素）
    if elem_cit.text and elem_cit.text.strip():
        parts.append(_normalize_ws(elem_cit.text))
    for c in elem_cit:
        tag = _localname(c.tag)
        if tag == "person-group":
            names = []
            for nm in c.iter("name"):
                given = next((x for x in nm if _localname(x.tag) == "given-names"), None)
                surname = next((x for x in nm if _localname(x.tag) == "surname"), None)
                g = _normalize_ws("".join(given.itertext())) if given is not None else ""
                sn = _normalize_ws("".join(surname.itertext())) if surname is not None else ""
                # CJK 姓氏（中文被引文献的混排元数据，2026-08-27 实测 SN ref7）用姓前名后，
                # 与原文排版一致；西文维持名前姓后
                if sn and re.search(r"[一-鿿]", sn):
                    full = f"{sn} {g}".strip()
                else:
                    full = " ".join(x for x in (g, sn) if x)
                if full:
                    names.append(full)
            if names:
                parts.append(", ".join(names))
        else:
            t = _normalize_ws("".join(c.itertext()))
            if t:
                parts.append(t)
        if c.tail and c.tail.strip():
            parts.append(_normalize_ws(c.tail))
    return " ".join(parts)


def _ref_payload_jats(root: ET.Element) -> dict | None:
    ref_list = next(root.iter("ref-list"), None)
    if ref_list is None:
        return None
    refs = []
    for i, ref in enumerate(ref_list.iter("ref"), 1):
        label = next((c for c in ref if _localname(c.tag) == "label"), None)
        label_t = _normalize_ws("".join(label.itertext())) if label is not None else ""
        n = None
        if label_t and re.fullmatch(r"\d+", label_t):
            n = int(label_t)
        n = n if n is not None else i
        # raw 与结构化提取共用：SN 现行 ref 用 mixed-citation，内部结构与 element-citation
        # 同构（person-group/pub-id/source/year）——纳入 elem_cit 候选，raw 走 _citation_raw
        # 重组（itertext 拍平会把姓名粘成 "GoelGSharmaM"，2026-08-27 Carbon Neutrality 实测）
        # 优先 element-citation（结构最规范）；mixed-citation 兜底（SN 现行标签，
        # 内部同构但部分老存档的 mixed 只是拍平文本）
        # 按优先级逐个找（next+集合是文档序优先，mixed 排前会被先选中——2026-08-27 实测踩坑）
        elem_cit = None
        for _cit_tag in ("element-citation", "citation", "nlm-citation", "mixed-citation"):
            elem_cit = next((c for c in ref if _localname(c.tag) == _cit_tag), None)
            if elem_cit is not None:
                break
        if elem_cit is not None:
            raw = _citation_raw(elem_cit)
        else:
            raw = ""
        if not raw:
            continue
        item = {"n": n, "raw": f"[{n}] {raw}"}
        if elem_cit is not None:
            # 注意：ElementTree 无子节点的 Element 是假值，元素查找一律显式 None 判断，禁用 or 链
            t = next(elem_cit.iter("article-title"), None)
            if t is None:
                t = next(elem_cit.iter("source"), None)
            if t is None:
                t = next(elem_cit.iter("chapter-title"), None)
            if t is not None:
                item["title"] = _normalize_ws("".join(t.itertext()))
            doi_el = next((e for e in elem_cit.iter("pub-id")
                           if e.get("pub-id-type") == "doi"), None)
            if doi_el is None:
                doi_el = next((e for e in elem_cit.iter("article-id")
                               if e.get("pub-id-type") == "doi"), None)
            if doi_el is not None:
                item["doi"] = _normalize_ws("".join(doi_el.itertext()))
            year_el = next(elem_cit.iter("year"), None)
            if year_el is not None:
                m = re.search(r"\d{4}", "".join(year_el.itertext()))
                if m:
                    item["year"] = int(m.group(0))
            venue = next(elem_cit.iter("source"), None)
            if venue is not None:
                item["venue"] = _normalize_ws("".join(venue.itertext()))
            authors = []
            for pg in elem_cit.iter("person-group"):
                for nm in pg:
                    if _localname(nm.tag) not in ("name", "string-name"):
                        continue
                    given = next((c for c in nm if _localname(c.tag) == "given-names"), None)
                    surname = next((c for c in nm if _localname(c.tag) == "surname"), None)
                    full = " ".join(x for x in (
                        _normalize_ws("".join(given.itertext())) if given is not None else "",
                        _normalize_ws("".join(surname.itertext())) if surname is not None else "") if x)
                    if full:
                        authors.append(full)
            if authors:
                item["authors"] = authors[:20]
        if "doi" not in item:
            # mixed-citation 无 element-citation 结构时，DOI 正则兜底（PDF 路径同款口径）
            m = re.search(r"(?:doi:\s*|https?://doi\.org/)(10\.\d{4,5}/[^\s,;)]+)",
                          raw, re.I)
            if m:
                item["doi"] = m.group(1).rstrip(".")
        refs.append(item)
    if not refs:
        return None
    return {"version": 1, "source": "xml", "count": len(refs), "references": refs}


def _ref_payload_elsevier(root: ET.Element) -> dict | None:
    bib = next((e for e in root.iter("bibliography")), None)
    if bib is None:
        return None
    refs = []
    for i, ref in enumerate(bib.iter("bib-reference"), 1):
        label = next((c for c in ref if _localname(c.tag) == "label"), None)
        label_t = _normalize_ws("".join(label.itertext())) if label is not None else ""
        n = int(label_t) if label_t.isdigit() else i
        textref = next(ref.iter("textref"), None)
        raw = _normalize_ws("".join(textref.itertext())) if textref is not None else \
            _normalize_ws("".join(ref.itertext()))
        raw = raw.removeprefix(label_t).strip() if label_t and raw.startswith(label_t) else raw
        if not raw:
            continue
        item = {"n": n, "raw": f"[{n}] {raw}"}
        m = re.search(r"(?:doi[:\s/]*|https?://doi\.org/)(10\.\d{4,5}/[^\s,;)]+)", raw, re.I)
        if m:
            item["doi"] = m.group(1).rstrip(".")
        refs.append(item)
    if not refs:
        return None
    return {"version": 1, "source": "xml", "count": len(refs), "references": refs}


# ============================================================
# parse()：staging 产物落盘（与 PDF 引擎 staging 同构）
# ============================================================

def parse(xml_path, staging_dir, progress=None) -> Path:
    """XML → staging 三件套。返回 content_list 路径（与 ocr_provider.parse 协议对齐）。"""
    xml_path = Path(xml_path)
    staging_dir = Path(staging_dir)
    staging_dir.mkdir(parents=True, exist_ok=True)

    if progress:
        progress("解析 XML 结构（JATS/Elsevier 方言检测）", 0.2)
    root = load_xml(xml_path)
    flavor = detect_flavor(root)
    logger.info(f"  XML 方言: {flavor}")

    meta = _jats_meta(root) if flavor == "jats" else _elsevier_meta(root)

    if progress:
        progress(f"提取正文（{flavor}）", 0.5)
    images_dir = staging_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    walker = _Walker(xml_path.parent, doi=meta.get("doi", ""), media_dir=images_dir)
    if flavor == "jats":
        body = next(root.iter("body"), None)
        if body is not None:
            for child in body:
                tag = _localname(child.tag)
                if tag == "sec":
                    _walk_jats_sec(child, walker, 1)
                elif tag == "p":
                    walker.paragraph(child)
            # 附录（back/app）按同级章节处理
            # 脚注：author-notes（通讯作者邮箱等，front 内）与 back/fn-group——
            # PMC 实测 author-notes 内嵌 fn（此前被静默丢弃）
            for group_tag in ("author-notes", "fn-group"):
                for group in root.iter(group_tag):
                    for fn in group.iter("fn"):
                        walker.footnote(fn)
            back = next(root.iter("back"), None)
            if back is not None:
                for app in back:
                    if _localname(app.tag) in ("app", "app-group"):
                        title = next(app.iter("title"), None)
                        walker.heading(_normalize_ws("".join(title.itertext()))
                                       if title is not None else "Appendix", 1)
                        for sec in app.iter("sec"):
                            _walk_jats_sec(sec, walker, 2)
    else:
        _walk_elsevier(root, walker)

    refs_payload = _ref_payload_jats(root) if flavor == "jats" else _ref_payload_elsevier(root)
    if refs_payload is not None:
        # References 章节块（正文流内可见 + 结构化 payload 双产物，与 PDF 路径同构）
        walker.heading("References", 1)
        for item in refs_payload["references"]:
            walker.blocks.append({"type": "ref_text", "text": item["raw"], "page_idx": 0})

    if progress:
        progress("落盘 staging 产物", 0.8)
    for name, src in walker.images.items():
        if src.parent != images_dir:  # SN 远端下载的直接落在 staging，防同路径自拷
            shutil.copy2(src, images_dir / name)

    cl_path = staging_dir / f"{xml_path.stem}_content_list.json"
    cl_path.write_text(json.dumps(walker.blocks, ensure_ascii=False, indent=1),
                       encoding="utf-8", newline="\n")
    (staging_dir / "xml_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8", newline="\n")
    (staging_dir / "xml_references.json").write_text(
        json.dumps(refs_payload, ensure_ascii=False, indent=1) if refs_payload else "null",
        encoding="utf-8", newline="\n")

    degraded = walker.degraded_figures + walker.degraded_formulas
    if degraded:
        logger.warning(f"  XML 降级块: 图 {walker.degraded_figures} 个 / 公式 "
                       f"{walker.degraded_formulas} 个（外链资源不可得，文本占位保底）")
    logger.info(f"  XML 解析完成: {len(walker.blocks)} 块 / 图片 {len(walker.images)} 张"
                f"（远端下载 {walker.remote_images}）/ "
                f"参考文献 {refs_payload['count'] if refs_payload else 0} 条")
    return cl_path
