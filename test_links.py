"""P1 原生链接保留（link_extractor）单元测试。

用 fitz 现场合成带链接注释的 PDF（insert_text + insert_link），
构造与 PDF 文本一致的最终块（ProcessedBlock 带 src_page），
覆盖：链接提取 / 目标映射（ref/fig/sec）/ 区间注入（同块多个 [12] 各自对准）/
数学段跳过 / URI 白名单 / 重复注释去重 / named dest 解析失败放弃。
"""

import os
import tempfile
import unittest
from pathlib import Path

import fitz

import link_extractor as le
from content_processor import ProcessedBlock, process_content
from link_extractor import (
    _skeletonize,
    block_anchor_id,
    collect_paper_links,
    extract_pdf_links,
)
from renderer import render_paper


def _make_pdf(spec: list[dict]) -> Path:
    """按规格合成 PDF。spec 每页: {"texts": [(x, y, str)], "links": [dict]}，
    links 的 "from_text" 表示用 search_for 定位源矩形（occurrence 选第几个）。
    """
    doc = fitz.open()
    for ps in spec:
        page = doc.new_page()
        for x, y, t in ps.get("texts", []):
            page.insert_text((x, y), t, fontsize=11)
    for pno, ps in enumerate(spec):
        page = doc.load_page(pno)  # new_page 后旧 Page 句柄可能失效，重新加载
        for lk in ps.get("links", []):
            rects = page.search_for(lk["from_text"])
            rect = rects[lk.get("occurrence", 0)]
            d = {"kind": lk["kind"], "from": rect}
            if lk["kind"] == fitz.LINK_URI:
                d["uri"] = lk["uri"]
            elif lk["kind"] == fitz.LINK_GOTO:
                d["page"] = lk["page"]
                d["to"] = fitz.Point(lk["to"])
            elif lk["kind"] == fitz.LINK_NAMED:
                d["nameddest"] = lk["nameddest"]
            page.insert_link(d)
    fd, path = tempfile.mkstemp(suffix=".pdf")
    os.close(fd)  # save 需重写该文件，先释放句柄（Windows 占用即拒）
    doc.save(path)
    doc.close()
    return Path(path)


class TestSkeletonize(unittest.TestCase):
    def test_fold_case_ligature_ws(self):
        sk, s2o = _skeletonize("A  B\u2019s \ufb01x")
        self.assertEqual(sk, "a b's fix")
        self.assertEqual(len(s2o, ), len(sk))
        # 骨架末字符 'x' 来自原文末字符
        self.assertEqual(s2o[-1], len("A  B\u2019s \ufb01x") - 1)

    def test_empty_and_ws(self):
        self.assertEqual(_skeletonize("   ")[0], "")
        self.assertEqual(_skeletonize("")[0], "")


class TestAnchorIds(unittest.TestCase):
    def test_ref(self):
        self.assertEqual(
            block_anchor_id(ProcessedBlock("reference", content="[12] X. et al.")), "ref-12")
        self.assertEqual(
            block_anchor_id(ProcessedBlock("reference", content="12. X. et al.")), "ref-12")
        self.assertIsNone(
            block_anchor_id(ProcessedBlock("reference", content="X. et al., no number")))

    def test_fig_tab(self):
        self.assertEqual(
            block_anchor_id(ProcessedBlock("image", content="Figure 3: A plot")), "fig-3")
        self.assertEqual(
            block_anchor_id(ProcessedBlock("table", content="<table/>",
                                           caption="Table 2: Data")), "tab-2")
        self.assertIsNone(block_anchor_id(ProcessedBlock("image", content="Figure X1")))

    def test_sec(self):
        self.assertEqual(
            block_anchor_id(ProcessedBlock("heading", content="II. Methods")),
            "sec-ii-methods")
        self.assertEqual(
            block_anchor_id(ProcessedBlock("heading", content="References")),
            "sec-references")

    def test_eq(self):
        self.assertEqual(
            block_anchor_id(ProcessedBlock(
                "equation", content="$$\nE = mc^2 \\tag{5}\n$$")), "eq-5")
        # 附录形态编号
        self.assertEqual(
            block_anchor_id(ProcessedBlock(
                "equation", content="$$\n\\alpha' \\tag{A2}\n$$")), "eq-A2")
        # 同块多 \tag：按既有去重规则取内容最长者（并列取最末）
        self.assertEqual(
            block_anchor_id(ProcessedBlock(
                "equation", content="$$\nx \\tag{5}\\tag{50}\n$$")), "eq-50")
        # 无 \tag → 无锚点
        self.assertIsNone(
            block_anchor_id(ProcessedBlock("equation", content="$$\nE=mc^2\n$$")))


class TestExtractAndInject(unittest.TestCase):
    """合成两篇 PDF：正文页（page0）+ 参考文献/图注页（page1）。"""

    def _cite_pdf(self) -> Path:
        return _make_pdf([
            {"texts": [
                (72, 100, "See the details in [12] and also [12] below."),
                (72, 120, "Data at https://doi.org/10.1234/foo now."),
                (72, 140, "Terms of Service applies here."),
                (72, 160, "As shown in Fig. 3 the curve rises; Fig. 4 differs."),
                (72, 180, "In Section II we describe it."),
                (72, 200, "Energy E = [5] exactly rises and [5] agrees."),
            ],
             "links": [
                {"kind": fitz.LINK_GOTO, "from_text": "[12]", "occurrence": 0,
                 "page": 1, "to": (72, 201)},
                {"kind": fitz.LINK_GOTO, "from_text": "[12]", "occurrence": 1,
                 "page": 1, "to": (72, 201)},
                {"kind": fitz.LINK_URI, "from_text": "https://doi.org/10.1234/foo",
                 "uri": "https://doi.org/10.1234/foo"},
                {"kind": fitz.LINK_URI, "from_text": "Terms of Service",
                 "uri": "http://www.example.com/about/terms-service"},
                {"kind": fitz.LINK_GOTO, "from_text": "Fig. 3",
                 "page": 1, "to": (72, 151)},
                {"kind": fitz.LINK_GOTO, "from_text": "Fig. 4",
                 "page": 1, "to": (72, 151)},  # 指向 Fig.3 图注 → 编号对不上，应放弃
                {"kind": fitz.LINK_GOTO, "from_text": "Section II",
                 "page": 1, "to": (72, 51)},
                {"kind": fitz.LINK_GOTO, "from_text": "[5]", "occurrence": 0,
                 "page": 1, "to": (72, 121)},
                {"kind": fitz.LINK_GOTO, "from_text": "[5]", "occurrence": 1,
                 "page": 1, "to": (72, 121)},
                {"kind": fitz.LINK_NAMED, "from_text": "[12]", "occurrence": 0,
                 "nameddest": "nonexistent.dest"},  # 解析失败 → 提取层放弃
            ]},
            {"texts": [
                (72, 50, "II. Methods"),
                (72, 100, "References"),
                (72, 120, "[5] Y. Author, Earlier Work."),
                (72, 150, "Figure 3: A sample plot."),
                (72, 200, "[12] X. Author, Great Title."),
            ]},
        ])

    def _cite_blocks(self, with_math: bool = False) -> list:
        para_math = ("Energy $E = [5] exactly$ rises and [5] agrees." if with_math
                     else "Energy E = [5] exactly rises and [5] agrees.")
        return [
            ProcessedBlock("heading", content="I. Intro", src_page=0),
            ProcessedBlock("paragraph",
                           content="See the details in [12] and also [12] below.",
                           src_page=0),
            ProcessedBlock("paragraph",
                           content="Data at https://doi.org/10.1234/foo now.",
                           src_page=0),
            ProcessedBlock("paragraph", content="Terms of Service applies here.",
                           src_page=0),
            ProcessedBlock("paragraph",
                           content="As shown in Fig. 3 the curve rises; Fig. 4 differs.",
                           src_page=0),
            ProcessedBlock("paragraph", content="In Section II we describe it.",
                           src_page=0),
            ProcessedBlock("paragraph", content=para_math, src_page=0),
            ProcessedBlock("heading", content="II. Methods", src_page=1),
            ProcessedBlock("heading", content="References", src_page=1),
            ProcessedBlock("reference", content="[5] Y. Author, Earlier Work.",
                           src_page=1),
            ProcessedBlock("image", content="Figure 3: A sample plot.",
                           img_new_name="fig3.jpg", src_page=1),
            ProcessedBlock("reference", content="[12] X. Author, Great Title.",
                           src_page=1),
        ]

    def test_extract_goto_uri(self):
        pdf = self._cite_pdf()
        extracted = extract_pdf_links(pdf)
        self.assertIsNotNone(extracted)
        pages, links = extracted
        # 9 条有效（named 解析失败的在提取层即放弃）
        self.assertEqual(len(links), 9)
        texts = sorted(lk.text for lk in links)
        self.assertIn("[12]", texts)
        self.assertIn("https://doi.org/10.1234/foo", texts)
        pdf.unlink()

    def test_inject_citations_twice_same_block(self):
        pdf = self._cite_pdf()
        blocks = self._cite_blocks()
        res = collect_paper_links(blocks, pdf)
        self.assertIsNotNone(res)
        self.assertEqual(
            blocks[1].content,
            "See the details in [[12]](#ref-12) and also [[12]](#ref-12) below.")
        self.assertIn("ref-12", res.anchors)
        pdf.unlink()

    def test_uri_whitelist(self):
        pdf = self._cite_pdf()
        blocks = self._cite_blocks()
        res = collect_paper_links(blocks, pdf)
        # DOI 文字链接注入；"Terms of Service" 杂链被白名单挡掉
        self.assertEqual(
            blocks[2].content,
            "Data at [https://doi.org/10.1234/foo](https://doi.org/10.1234/foo) now.")
        self.assertEqual(blocks[3].content, "Terms of Service applies here.")
        uri_stat = res.stats.get("uri", [0, 0])
        self.assertEqual(uri_stat, [1, 1])
        pdf.unlink()

    def test_fig_and_mismatch_drop(self):
        pdf = self._cite_pdf()
        blocks = self._cite_blocks()
        res = collect_paper_links(blocks, pdf)
        # Fig. 3 注入；Fig. 4 编号对不上（位置映射到 Figure 3 图注）→ 放弃
        self.assertEqual(
            blocks[4].content,
            "As shown in [Fig. 3](#fig-3) the curve rises; Fig. 4 differs.")
        self.assertIn("fig-3", res.anchors)
        fig_stat = res.stats.get("fig", [0, 0])
        self.assertEqual(fig_stat, [1, 1])
        pdf.unlink()

    def test_section_link(self):
        pdf = self._cite_pdf()
        blocks = self._cite_blocks()
        res = collect_paper_links(blocks, pdf)
        self.assertEqual(blocks[5].content,
                         "In [Section II](#sec-ii-methods) we describe it.")
        self.assertIn("sec-ii-methods", res.anchors)
        pdf.unlink()

    def test_math_span_skipped(self):
        pdf = self._cite_pdf()
        blocks = self._cite_blocks(with_math=True)
        res = collect_paper_links(blocks, pdf)
        # 数学段 $...$ 内的 [5] 不注入；数学段外的 [5] 正常注入
        self.assertEqual(
            blocks[6].content,
            "Energy $E = [5] exactly$ rises and [[5]](#ref-5) agrees.")
        pdf.unlink()

    def test_equation_goto_positional(self):
        # GOTO 直坐标记落到展示公式行：位置映射命中（公式块内容含原文片段）
        pdf = _make_pdf([
            {"texts": [(72, 100, "As shown in (5) the relation holds.")],
             "links": [{"kind": fitz.LINK_GOTO, "from_text": "(5)",
                        "page": 1, "to": (72, 101)}]},
            {"texts": [(72, 100, "E = mc^2 (5)")]},
        ])
        blocks = [
            ProcessedBlock("paragraph",
                           content="As shown in (5) the relation holds.", src_page=0),
            ProcessedBlock("equation", content="$$\nE = mc^2 \\tag{5}\n$$", src_page=1),
        ]
        res = collect_paper_links(blocks, pdf)
        self.assertEqual(blocks[0].content,
                         "As shown in [(5)](#eq-5) the relation holds.")
        self.assertIn("eq-5", res.anchors)
        pdf.unlink()

    def test_equation_dest_mismatch_dropped(self):
        # 位置映射落空（公式 LaTeX 化对不齐）且同号 \tag 不唯一/不存在 → 放弃
        pdf = _make_pdf([
            {"texts": [(72, 100, "As shown in (7) the relation holds.")],
             "links": [{"kind": fitz.LINK_GOTO, "from_text": "(7)",
                        "page": 1, "to": (72, 101)}]},
            {"texts": [(72, 100, "E = mc^2 (5)")]},
        ])
        blocks = [
            ProcessedBlock("paragraph",
                           content="As shown in (7) the relation holds.", src_page=0),
            ProcessedBlock("equation", content="$$\nE = mc^2 \\tag{5}\n$$", src_page=1),
        ]
        res = collect_paper_links(blocks, pdf)
        self.assertEqual(blocks[0].content, "As shown in (7) the relation holds.")
        self.assertNotIn("eq-5", res.anchors)
        pdf.unlink()

    def test_equation_no_tag_dropped(self):
        # 位置映射命中的是无 \tag 公式块 → 解析不到编号，放弃保纯文本
        pdf = _make_pdf([
            {"texts": [(72, 100, "As shown in (5) the relation holds.")],
             "links": [{"kind": fitz.LINK_GOTO, "from_text": "(5)",
                        "page": 1, "to": (72, 101)}]},
            {"texts": [(72, 100, "E = mc^2")]},
        ])
        blocks = [
            ProcessedBlock("paragraph",
                           content="As shown in (5) the relation holds.", src_page=0),
            ProcessedBlock("equation", content="$$\nE = mc^2\n$$", src_page=1),
        ]
        res = collect_paper_links(blocks, pdf)
        self.assertEqual(blocks[0].content, "As shown in (5) the relation holds.")
        pdf.unlink()

    def test_duplicate_annotation_dedup(self):
        pdf = _make_pdf([
            {"texts": [(72, 100, "See [12] here.")],
             "links": [
                {"kind": fitz.LINK_GOTO, "from_text": "[12]", "page": 1, "to": (72, 101)},
                {"kind": fitz.LINK_GOTO, "from_text": "[12]", "page": 1, "to": (72, 101)},
            ]},
            {"texts": [(72, 100, "[12] X. Author, Great Title.")]},
        ])
        blocks = [
            ProcessedBlock("paragraph", content="See [12] here.", src_page=0),
            ProcessedBlock("reference", content="[12] X. Author, Great Title.",
                           src_page=1),
        ]
        res = collect_paper_links(blocks, pdf)
        self.assertEqual(blocks[0].content, "See [[12]](#ref-12) here.")
        self.assertEqual(res.injected, 1)
        pdf.unlink()

    def test_unbalanced_bracket_display_dropped(self):
        # 跨行链接矩形切进 "[astro-" 之类不平衡括号 → 放弃（防破坏 Markdown 语法）
        pdf = _make_pdf([
            {"texts": [(72, 100, "See arXiv:2404.13213 [astro-ph.CO] here.")],
             "links": [{"kind": fitz.LINK_URI,
                        "from_text": "arXiv:2404.13213 [astro-ph",
                        "uri": "https://arxiv.org/abs/2404.13213"}]},
        ])
        blocks = [ProcessedBlock(
            "paragraph", content="See arXiv:2404.13213 [astro-ph.CO] here.",
            src_page=0)]
        res = collect_paper_links(blocks, pdf)
        self.assertEqual(blocks[0].content,
                         "See arXiv:2404.13213 [astro-ph.CO] here.")
        self.assertEqual(res.injected, 0)
        pdf.unlink()

    def test_adjacent_same_target_merged(self):
        # 同 URI 的相邻矩形（跨行拆链）合并为一条链接，方括号恢复平衡
        pdf = _make_pdf([
            {"texts": [(72, 100, "See arXiv:2404.13213 [astro-ph.CO] here.")],
             "links": [
                {"kind": fitz.LINK_URI, "from_text": "arXiv:2404.13213 [astro-",
                 "uri": "https://arxiv.org/abs/2404.13213"},
                {"kind": fitz.LINK_URI, "from_text": "ph.CO]",
                 "uri": "https://arxiv.org/abs/2404.13213"},
            ]},
        ])
        blocks = [ProcessedBlock(
            "paragraph", content="See arXiv:2404.13213 [astro-ph.CO] here.",
            src_page=0)]
        res = collect_paper_links(blocks, pdf)
        self.assertEqual(
            blocks[0].content,
            "See [arXiv:2404.13213 [astro-ph.CO]](https://arxiv.org/abs/2404.13213) here.")
        pdf.unlink()

    def test_no_links_pdf_returns_none(self):
        pdf = _make_pdf([{"texts": [(72, 100, "Plain text, no annotations.")]}])
        self.assertIsNone(collect_paper_links(
            [ProcessedBlock("paragraph", content="Plain text, no annotations.",
                            src_page=0)], pdf))
        pdf.unlink()

    def test_unnumbered_ref_entry_dropped(self):
        # 无编号条目无法校验编号一致性 → 放弃（宁缺毋滥）
        pdf = _make_pdf([
            {"texts": [(72, 100, "See [7] here.")],
             "links": [{"kind": fitz.LINK_GOTO, "from_text": "[7]",
                        "page": 1, "to": (72, 101)}]},
            {"texts": [(72, 100, "Smith et al., Some Paper, 2020.")]},
        ])
        blocks = [
            ProcessedBlock("paragraph", content="See [7] here.", src_page=0),
            ProcessedBlock("reference", content="Smith et al., Some Paper, 2020.",
                           src_page=1),
        ]
        res = collect_paper_links(blocks, pdf)
        self.assertEqual(blocks[0].content, "See [7] here.")
        self.assertEqual(res.injected, 0)
        self.assertEqual(res.dropped, 1)
        pdf.unlink()


class TestCitationMathUnwrap(unittest.TestCase):
    """引文簇误判行内公式拆 $ 壳（$[2, 3]$ → [2, 3]；真数学段不动）。"""

    def test_unwrap_cluster(self):
        from content_processor import _normalize_inline
        self.assertEqual(_normalize_inline("see $[2, 3]$ for a review"),
                         "see [2, 3] for a review")
        self.assertEqual(_normalize_inline("scenarios $[4\u20138]$ --- are"),
                         "scenarios [4\u20138] --- are")
        self.assertEqual(_normalize_inline("$[15, 19\u201322]$"), "[15, 19\u201322]")

    def test_real_math_untouched(self):
        from content_processor import _normalize_inline
        self.assertEqual(_normalize_inline("energy $E = [5] exactly$ rises"),
                         "energy $E = [5] exactly$ rises")
        self.assertEqual(_normalize_inline("value $x_1$ here"), "value $x_1$ here")

    def test_unwrapped_cluster_injectable(self):
        # 拆壳后簇链接按原始映射逐数字成链（此前被数学段规则整体 veto）。
        # 真实场景是 hyperref 的 cite.* named dest（数字紧贴矩形），
        # 用合成 named 链接注入（dest_page=-1 → 同号唯一条目兜底解析）
        doc = fitz.open()
        doc.new_page().insert_text((72, 100), "See [2, 3] for a review.", fontsize=11)
        fd, path = tempfile.mkstemp(suffix=".pdf")
        os.close(fd)
        doc.save(path)
        doc.close()
        doc = fitz.open(path)
        pages = [le._Page(doc[0])]
        links = []
        for digit, dest in (("2", "cite.a"), ("3", "cite.b")):
            c0 = pages[0].raw.find(digit)
            links.append(le._Link(0, c0, c0 + 1, digit, dest_name=dest))
        orig = le.extract_pdf_links
        le.extract_pdf_links = lambda _pdf: (pages, links)
        blocks = [
            ProcessedBlock("paragraph", content="See [2, 3] for a review.", src_page=0),
            ProcessedBlock("reference", content="[2] X. Author, T.", src_page=0),
            ProcessedBlock("reference", content="[3] Y. Author, U.", src_page=0),
        ]
        try:
            res = collect_paper_links(blocks, path)
        finally:
            le.extract_pdf_links = orig
            doc.close()
        Path(path).unlink()
        self.assertEqual(blocks[0].content,
                         "See [[2](#ref-2), [3](#ref-3)] for a review.")
        self.assertEqual(res.stats.get("ref"), [2, 0])


class TestFootnotePreserved(unittest.TestCase):
    """page_footnote 不再丢弃（文本零丢失红线）：落成独立 footnote 块、
    不参与段落合并、渲染为普通段落。"""

    def test_footnote_block_survives(self):
        cl = [
            {"type": "text", "text": "We derive analytical approximations",
             "page_idx": 0},
            {"type": "page_footnote",
             "text": "\\* Electronic address: sergei@astro.up.pt", "page_idx": 0},
            {"type": "text", "text": "for the full model here.", "page_idx": 0},
        ]
        from content_processor import process_content
        blocks = process_content(cl, use_llm=False, title="Cosmic strings")
        kinds = [b.kind for b in blocks]
        self.assertIn("footnote", kinds)
        fn = blocks[kinds.index("footnote")]
        self.assertIn("Electronic address", fn.content)
        self.assertEqual(fn.src_page, 0)
        # 段落合并不把脚注粘进正文（正文两段被脚注隔开，各自独立）
        paras = [b for b in blocks if b.kind == "paragraph"]
        self.assertFalse(any("Electronic address" in b.content for b in paras))

    def test_footnote_renders(self):
        blocks = [
            ProcessedBlock("paragraph", content="Body text."),
            ProcessedBlock("footnote",
                           content="* Electronic address: sergei@astro.up.pt"),
        ]
        with tempfile.TemporaryDirectory() as td:
            md = render_paper(
                blocks=blocks,
                metadata={"title": "T", "author": [{"name": "A"}], "date": "2024"},
                output_dir=Path(td), slug="t")
            text = md.read_text(encoding="utf-8")
        self.assertIn("Electronic address: sergei@astro.up.pt", text)

    def test_footnote_uri_injectable(self):
        pdf = _make_pdf([
            {"texts": [(72, 100, "Contact sergei@astro.up.pt anytime.")],
             "links": [{"kind": fitz.LINK_URI, "from_text": "sergei@astro.up.pt",
                        "uri": "mailto:sergei@astro.up.pt"}]},
        ])
        blocks = [ProcessedBlock(
            "footnote", content="Contact sergei@astro.up.pt anytime.", src_page=0)]
        res = collect_paper_links(blocks, pdf)
        self.assertEqual(
            blocks[0].content,
            "Contact [sergei@astro.up.pt](mailto:sergei@astro.up.pt) anytime.")
        pdf.unlink()


class TestFootnoteLinks(unittest.TestCase):
    """P1 脚注链接追回：footnote/frontmatter 类 dest → 引用点 [^N] 注入。"""

    def _run(self, page0_text, link_specs, blocks):
        """link_specs: [(源文字, dest_name)]；dest_page=-1 → covering 无坐标。"""
        doc = fitz.open()
        doc.new_page().insert_text((72, 100), page0_text, fontsize=11)
        fd, path = tempfile.mkstemp(suffix=".pdf")
        os.close(fd)
        doc.save(path)
        doc.close()
        doc = fitz.open(path)
        pages = [le._Page(doc[0])]
        links = []
        for txt, dest in link_specs:
            c0 = pages[0].raw.find(txt)
            self.assertGreaterEqual(c0, 0, f"{txt!r} 不在页文本里")
            links.append(le._Link(0, c0, c0 + len(txt), txt, dest_name=dest))
        orig = le.extract_pdf_links
        le.extract_pdf_links = lambda _pdf: (pages, links)
        try:
            res = collect_paper_links(blocks, path)
        finally:
            le.extract_pdf_links = orig
            doc.close()
        Path(path).unlink()
        return res

    def _fn_block(self, num, text="Note that, the direction of degeneracy."):
        b = ProcessedBlock("footnote", content=text, src_page=0)
        b.note_num = num
        return b

    def test_numbered_footnote_ref_injected(self):
        # 全链路：Hfootnote.7（计数器 ≠ 印刷编号）可见标记 '6' → 目标脚注块
        # note_num=6 → 正文标记替换为 [^6]（编号两端同号）
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 100), "As shown before6 in the text.", fontsize=11)
        page.insert_text((72, 700), "6 Note that, the direction of degeneracy.", fontsize=11)
        fd, path = tempfile.mkstemp(suffix=".pdf")
        os.close(fd)
        doc.save(path)
        doc.close()
        doc = fitz.open(path)
        pages = [le._Page(doc[0])]
        c0 = pages[0].raw.find("before") + len("before")
        links = [le._Link(0, c0, c0 + 1, "6", dest_name="Hfootnote.7",
                          dest_page=0, dest_x=72.0, dest_y=700.0)]
        orig = le.extract_pdf_links
        le.extract_pdf_links = lambda _pdf: (pages, links)
        blocks = [ProcessedBlock("paragraph",
                                 content="As shown before6 in the text.", src_page=0),
                  self._fn_block(6)]
        try:
            res = collect_paper_links(blocks, path)
        finally:
            le.extract_pdf_links = orig
            doc.close()
        Path(path).unlink()
        self.assertEqual(blocks[0].content, "As shown before[^6] in the text.")
        self.assertEqual(res.stats.get("fn"), [1, 0])

    def test_target_not_footnote_dropped(self):
        # 目标落在普通段落而非 footnote 块 → 放弃
        blocks = [ProcessedBlock("paragraph",
                                 content="...as shown before6 in the text.",
                                 src_page=0)]
        res = self._run("...as shown before6 in the text.",
                        [("6", "Hfootnote.2")], blocks)
        self.assertEqual(blocks[0].content, "...as shown before6 in the text.")
        self.assertEqual(res.stats.get("fn"), [0, 1])

    def test_number_mismatch_dropped(self):
        # 源标记 5 但目标脚注块编号 6 → 编号对不上 → 放弃
        blocks = [ProcessedBlock("paragraph", content="text5 marker.", src_page=0),
                  self._fn_block(6)]
        res = self._run("text5 marker.", [("5", "Hfootnote.6")], blocks)
        self.assertEqual(blocks[0].content, "text5 marker.")
        self.assertEqual(res.stats.get("fn"), [0, 1])

    def test_math_swallowed_marker_split(self):
        # forecast eq12 原形：PDF 文本是 "...f^{-4/3}2 after"（2 为上标脚注标记，
        # 带链接），引擎归一成 $f^{-4/32}$ → 证据驱动剥离为 $f^{-4/3}$[^2]
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 100), "in proportion to f^{-4/3}2 after the burst.",
                         fontsize=11)
        page.insert_text((72, 700), "2 Strictly speaking, Eq. (12) is valid.",
                         fontsize=11)
        fd, path = tempfile.mkstemp(suffix=".pdf")
        os.close(fd)
        doc.save(path)
        doc.close()
        doc = fitz.open(path)
        pages = [le._Page(doc[0])]
        c0 = pages[0].raw.find("}2") + 1  # 数学尾部被吞的 "2"
        links = [le._Link(0, c0, c0 + 1, "2", dest_name="Hfootnote.3",
                          dest_page=0, dest_x=72.0, dest_y=700.0)]
        orig = le.extract_pdf_links
        le.extract_pdf_links = lambda _pdf: (pages, links)
        blocks = [ProcessedBlock(
            "paragraph",
            content="in proportion to $f^{-4/32}$ after the burst.", src_page=0),
            self._fn_block(2, "Strictly speaking, Eq. (12) is valid.")]
        try:
            res = collect_paper_links(blocks, path)
        finally:
            le.extract_pdf_links = orig
            doc.close()
        Path(path).unlink()
        self.assertEqual(
            blocks[0].content,
            "in proportion to $f^{-4/3}$[^2] after the burst.")
        self.assertEqual(res.stats.get("fn"), [1, 0])

    def test_no_split_for_real_exponent(self):
        # 真指数 $x^2$：前置字符是 ^（非数字/}）→ 绝不拆
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 100), "the growth x2 after the burst.", fontsize=11)
        page.insert_text((72, 700), "2 Strictly speaking, Eq. (12) is valid.",
                         fontsize=11)
        fd, path = tempfile.mkstemp(suffix=".pdf")
        os.close(fd)
        doc.save(path)
        doc.close()
        doc = fitz.open(path)
        pages = [le._Page(doc[0])]
        c0 = pages[0].raw.find("x2") + 1
        links = [le._Link(0, c0, c0 + 1, "2", dest_name="Hfootnote.3",
                          dest_page=0, dest_x=72.0, dest_y=700.0)]
        orig = le.extract_pdf_links
        le.extract_pdf_links = lambda _pdf: (pages, links)
        blocks = [ProcessedBlock(
            "paragraph", content="the growth $x^2$ after the burst.", src_page=0),
            self._fn_block(2, "Strictly speaking, Eq. (12) is valid.")]
        try:
            res = collect_paper_links(blocks, path)
        finally:
            le.extract_pdf_links = orig
            doc.close()
        Path(path).unlink()
        self.assertEqual(blocks[0].content,
                         "the growth $x^2$ after the burst.")
        self.assertEqual(res.stats.get("fn"), [0, 1])

    def test_cross_boundary_sup_marker_recovered(self):
        # friction 篇原形：PDF 链接矩形覆盖 "s2,"（正文+上标+句读），引擎把上标
        # 归一为独立数学段 $^{2}$——锚区间跨文本/数学边界，严格/宽松两档对齐
        # 均落空。证据（字母前缀 + 句读后缀 + 独立 $^{N}$ 段形态 + 页内唯一）
        # 齐全 → 整段替换为 [^2]
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 100), "For standard local strings2, we assume.",
                         fontsize=11)
        page.insert_text((72, 700), "2 This is not the case here.", fontsize=11)
        fd, path = tempfile.mkstemp(suffix=".pdf")
        os.close(fd)
        doc.save(path)
        doc.close()
        doc = fitz.open(path)
        pages = [le._Page(doc[0])]
        c0 = pages[0].raw.find("strings2,") + len("string")  # 链接矩形覆盖 "s2,"
        links = [le._Link(0, c0, c0 + 3, "s2,", dest_name="equation.3-footnote.2",
                          dest_page=0, dest_x=72.0, dest_y=700.0)]
        orig = le.extract_pdf_links
        le.extract_pdf_links = lambda _pdf: (pages, links)
        blocks = [ProcessedBlock(
            "paragraph",
            content="For standard local strings $^{2}$ , we assume.", src_page=0),
            self._fn_block(2, "This is not the case here.")]
        try:
            res = collect_paper_links(blocks, path)
        finally:
            le.extract_pdf_links = orig
            doc.close()
        Path(path).unlink()
        self.assertEqual(blocks[0].content,
                         "For standard local strings [^2] , we assume.")
        self.assertEqual(res.stats.get("fn"), [1, 0])

    def test_cross_boundary_ambiguous_dropped(self):
        # 跨边界找回的多义守卫：锚点无字母前缀（'4.'）且页内有两个独立 $^{4}$
        # 段 → 多义放弃（维持原样，不损失信息）
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 100), "first mention4. and second mention4. end.",
                         fontsize=11)
        page.insert_text((72, 700), "4 Footnote text.", fontsize=11)
        fd, path = tempfile.mkstemp(suffix=".pdf")
        os.close(fd)
        doc.save(path)
        doc.close()
        doc = fitz.open(path)
        pages = [le._Page(doc[0])]
        c0 = pages[0].raw.find("mention4.") + len("mention")
        links = [le._Link(0, c0, c0 + 2, "4.", dest_name="equation.30-footnote.4",
                          dest_page=0, dest_x=72.0, dest_y=700.0)]
        orig = le.extract_pdf_links
        le.extract_pdf_links = lambda _pdf: (pages, links)
        blocks = [ProcessedBlock(
            "paragraph",
            content="first mention $^{4}$ . and second mention $^{4}$ . end.",
            src_page=0),
            self._fn_block(4, "Footnote text.")]
        try:
            res = collect_paper_links(blocks, path)
        finally:
            le.extract_pdf_links = orig
            doc.close()
        Path(path).unlink()
        self.assertEqual(blocks[0].content,
                         "first mention $^{4}$ . and second mention $^{4}$ . end.")
        self.assertEqual(res.stats.get("fn"), [0, 1])

    def test_cross_boundary_prefix_disambiguates(self):
        # 前缀证据的消歧价值：页内两个独立 $^{2}$ 段（strings² 与 university²），
        # 锚点字母前缀 's' 选中前文中字符串收尾者
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 100), "standard strings2 plus university2 addr.",
                         fontsize=11)
        page.insert_text((72, 700), "2 Footnote text.", fontsize=11)
        fd, path = tempfile.mkstemp(suffix=".pdf")
        os.close(fd)
        doc.save(path)
        doc.close()
        doc = fitz.open(path)
        pages = [le._Page(doc[0])]
        c0 = pages[0].raw.find("strings2") + len("string")
        links = [le._Link(0, c0, c0 + 2, "s2", dest_name="Hfootnote.2",
                          dest_page=0, dest_x=72.0, dest_y=700.0)]
        orig = le.extract_pdf_links
        le.extract_pdf_links = lambda _pdf: (pages, links)
        blocks = [ProcessedBlock(
            "paragraph",
            content="standard strings $^{2}$ plus university $^{2}$ addr.",
            src_page=0),
            self._fn_block(2, "Footnote text.")]
        try:
            res = collect_paper_links(blocks, path)
        finally:
            le.extract_pdf_links = orig
            doc.close()
        Path(path).unlink()
        self.assertEqual(blocks[0].content,
                         "standard strings [^2] plus university $^{2}$ addr.")
        self.assertEqual(res.stats.get("fn"), [1, 0])

    def test_resolve_fallback_when_covering_wrong_fn_block(self):
        # friction fn2 原形：covering_block 把 fn2 的 dest 映射到同页兄弟脚注块
        # fn1（y 邻近错配），编号失配后按"目标页同号 footnote 块唯一"兜底采纳 fn2
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 100), "For standard local strings2, we assume.",
                         fontsize=11)
        page.insert_text((72, 700), "1 First note about omission.", fontsize=11)
        page.insert_text((72, 720), "2 This is not the case here.", fontsize=11)
        fd, path = tempfile.mkstemp(suffix=".pdf")
        os.close(fd)
        doc.save(path)
        doc.close()
        doc = fitz.open(path)
        pages = [le._Page(doc[0])]
        c0 = pages[0].raw.find("strings2,") + len("string")
        # dest 指向 fn1 的行（y=700）——covering_block 会错配到 fn1 块
        links = [le._Link(0, c0, c0 + 2, "s2", dest_name="equation.3-footnote.2",
                          dest_page=0, dest_x=72.0, dest_y=700.0)]
        orig = le.extract_pdf_links
        le.extract_pdf_links = lambda _pdf: (pages, links)
        fn1 = self._fn_block(1, "First note about omission.")
        fn2 = self._fn_block(2, "This is not the case here.")
        blocks = [ProcessedBlock(
            "paragraph",
            content="For standard local strings $^{2}$ , we assume.", src_page=0),
            fn1, fn2]
        try:
            res = collect_paper_links(blocks, path)
        finally:
            le.extract_pdf_links = orig
            doc.close()
        Path(path).unlink()
        self.assertEqual(blocks[0].content,
                         "For standard local strings [^2] , we assume.")
        self.assertEqual(res.stats.get("fn"), [1, 0])

    def test_exponent_tail_marker_recovered(self):
        # friction fn7 原形：上标标记被引擎吞成"指数"——$g^{7}$。锚区间在
        # 等值段内不可映射（段芯 g^{7} 与页文本 g7 骨架错位），两档对齐与
        # math_hit 均落空；段芯以 ^{7} 收尾 + 页内唯一 → 拆出为 $g$[^7]
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 100), "the parameter g7 , which was set to 1.",
                         fontsize=11)
        page.insert_text((72, 700), "7 As before, we assume const.", fontsize=11)
        fd, path = tempfile.mkstemp(suffix=".pdf")
        os.close(fd)
        doc.save(path)
        doc.close()
        doc = fitz.open(path)
        pages = [le._Page(doc[0])]
        c0 = pages[0].raw.find("g7") + 1  # 标记数字 "7"
        links = [le._Link(0, c0, c0 + 1, "7", dest_name="equation.49-footnote.7",
                          dest_page=0, dest_x=72.0, dest_y=700.0)]
        orig = le.extract_pdf_links
        le.extract_pdf_links = lambda _pdf: (pages, links)
        blocks = [ProcessedBlock(
            "paragraph",
            content="the parameter $g^{7}$ , which was set to 1.", src_page=0),
            self._fn_block(7, "As before, we assume const.")]
        try:
            res = collect_paper_links(blocks, path)
        finally:
            le.extract_pdf_links = orig
            doc.close()
        Path(path).unlink()
        self.assertEqual(blocks[0].content,
                         "the parameter $g$[^7] , which was set to 1.")
        self.assertEqual(res.stats.get("fn"), [1, 0])

    def test_exponent_tail_real_exponent_safe(self):
        # 真指数 $x^{2}$ 但脚注编号是 3（段芯 ^{2} ≠ label 3）→ 拆不出，放弃
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 100), "the growth x3 , after.", fontsize=11)
        page.insert_text((72, 700), "3 Footnote text.", fontsize=11)
        fd, path = tempfile.mkstemp(suffix=".pdf")
        os.close(fd)
        doc.save(path)
        doc.close()
        doc = fitz.open(path)
        pages = [le._Page(doc[0])]
        c0 = pages[0].raw.find("x3") + 1
        links = [le._Link(0, c0, c0 + 1, "3", dest_name="Hfootnote.5",
                          dest_page=0, dest_x=72.0, dest_y=700.0)]
        orig = le.extract_pdf_links
        le.extract_pdf_links = lambda _pdf: (pages, links)
        blocks = [ProcessedBlock(
            "paragraph", content="the growth $x^{2}$ , after.", src_page=0),
            self._fn_block(3, "Footnote text.")]
        try:
            res = collect_paper_links(blocks, path)
        finally:
            le.extract_pdf_links = orig
            doc.close()
        Path(path).unlink()
        self.assertEqual(blocks[0].content, "the growth $x^{2}$ , after.")
        self.assertEqual(res.stats.get("fn"), [0, 1])

    def test_no_renumber_when_not_bijection(self):
        # 锚定保守策略：marker 2 缺失（被吞/未定位）时定义编号保持原样，
        # 绝不整体重排（[^3] 不得变 [^2]）
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 100), "text one1 and three3 end.", fontsize=11)
        page.insert_text((72, 690), "1 First note.", fontsize=11)
        page.insert_text((72, 710), "3 Third note.", fontsize=11)
        fd, path = tempfile.mkstemp(suffix=".pdf")
        os.close(fd)
        doc.save(path)
        doc.close()
        doc = fitz.open(path)
        pages = [le._Page(doc[0])]
        links = []
        for digit, dest, dy in (("1", "Hfootnote.2", 690.0),
                                ("3", "Hfootnote.4", 710.0)):
            c0 = pages[0].raw.find("one" + digit) + 3 if digit == "1" \
                else pages[0].raw.find("three" + digit) + 5
            links.append(le._Link(0, c0, c0 + 1, digit, dest_name=dest,
                                  dest_page=0, dest_x=72.0, dest_y=dy))
        orig = le.extract_pdf_links
        le.extract_pdf_links = lambda _pdf: (pages, links)
        blocks = [ProcessedBlock("paragraph",
                                 content="text one1 and three3 end.", src_page=0),
                  self._fn_block(1, "First note."),
                  self._fn_block(2, "Second note (marker lost)."),
                  self._fn_block(3, "Third note.")]
        try:
            res = collect_paper_links(blocks, path)
        finally:
            le.extract_pdf_links = orig
            doc.close()
        Path(path).unlink()
        self.assertIn("[^1]", blocks[0].content)
        self.assertIn("[^3]", blocks[0].content)  # 不重排
        self.assertEqual([b.note_num for b in blocks[1:]], [1, 2, 3])

    def test_fn_symbol_map(self):
        b = ProcessedBlock("footnote", content="\\* Electronic address: a@b.c")
        self.assertEqual(le._footnote_symbol_of(b), "*")
        b2 = ProcessedBlock("footnote", content="$^{\\dagger}$ Electronic address: a@b.c")
        self.assertEqual(le._footnote_symbol_of(b2), "†")
        b3 = ProcessedBlock("footnote", content="Note that something.")
        self.assertIsNone(le._footnote_symbol_of(b3))

    def test_fn_dest_recognition(self):
        for d in ("Hfootnote.2", "footnote.3", "equation.3-footnote.1",
                  "section*.6-footnote.3", "frontmatter.1", "MAC11592FN2", "fn1"):
            self.assertTrue(le._FN_DEST_RE.search(d), d)
        for d in ("cite.x", "figure.3", "bib0001", "aff0001", "cor0001", ""):
            self.assertFalse(le._FN_DEST_RE.search(d or "x"), d)


class TestCitationClusters(unittest.TestCase):
    """引文簇/区间部分矩形合并（monkeypatch 提取层注入合成的 cite.* 链接）。"""

    def _run(self, page0_text, part_specs, blocks):
        """part_specs: [(部分文字, dest_name)]；返回 (LinkResult, blocks)。"""
        doc = fitz.open()
        doc.new_page().insert_text((72, 100), page0_text, fontsize=11)
        doc.new_page().insert_text((72, 100), "refs", fontsize=11)
        fd, path = tempfile.mkstemp(suffix=".pdf")
        os.close(fd)
        doc.save(path)
        doc.close()
        doc = fitz.open(path)
        pages = [le._Page(doc[i]) for i in range(doc.page_count)]
        links = []
        for part, dest in part_specs:
            c0 = pages[0].raw.find(part)
            self.assertGreaterEqual(c0, 0, f"{part!r} 不在页文本里")
            links.append(le._Link(0, c0, c0 + len(part), part,
                                  dest_name=dest))  # dest_page=-1 → 走同号唯一兜底
        orig = le.extract_pdf_links
        le.extract_pdf_links = lambda _pdf: (pages, links)
        try:
            res = collect_paper_links(blocks, path)
        finally:
            le.extract_pdf_links = orig
            doc.close()
        Path(path).unlink()
        return res

    def _blocks(self, para_text, ref_nums=(2, 3, 4, 8)):
        bl = [ProcessedBlock("paragraph", content=para_text, src_page=0),
              ProcessedBlock("heading", content="References", src_page=1)]
        for n in ref_nums:
            bl.append(ProcessedBlock("reference",
                                     content=f"[{n}] X. Author, T.", src_page=1))
        return bl

    def test_cluster_and_range_merged(self):
        text = "Recent work [2, 3] and [4-8] shows."
        blocks = self._blocks(text)
        res = self._run(text, [("[2,", "cite.a"), ("3]", "cite.b"),
                               ("[4-", "cite.c"), ("-8]", "cite.d")], blocks)
        self.assertEqual(
            blocks[0].content,
            "Recent work [[2](#ref-2), [3](#ref-3)] and "
            "[[4](#ref-4)-[8](#ref-8)] shows.")
        self.assertEqual(res.stats.get("ref"), [4, 0])

    def test_cluster_rollback_when_member_unresolvable(self):
        # 成员 9 无对应条目 → 整组放弃（含本可单独成立的 [2）
        text = "Recent work [2, 9] shows."
        blocks = self._blocks(text, ref_nums=(2,))
        res = self._run(text, [("[2,", "cite.a"), ("9]", "cite.b")], blocks)
        self.assertEqual(blocks[0].content, text)
        self.assertEqual(res.stats.get("ref"), [0, 2])

    def test_no_false_merge_across_lines(self):
        # 两个部分矩形不在同一行（y 差超阈值）→ 不合并，各自维持放弃
        doc = fitz.open()
        doc.new_page().insert_text((72, 100), "First line [2, ends.", fontsize=11)
        page = doc.load_page(0)
        page.insert_text((72, 200), "Second line 3] here.", fontsize=11)
        fd, path = tempfile.mkstemp(suffix=".pdf")
        os.close(fd)
        doc.save(path)
        doc.close()
        doc = fitz.open(path)
        pages = [le._Page(doc[0])]
        c0a = pages[0].raw.find("[2,")
        c0b = pages[0].raw.find("3]")
        links = [le._Link(0, c0a, c0a + 3, "[2,", dest_name="cite.a"),
                 le._Link(0, c0b, c0b + 2, "3]", dest_name="cite.b")]
        merged = le._merge_citation_clusters(links, pages)
        self.assertTrue(all(lk.cluster < 0 for lk in merged))
        doc.close()
        Path(path).unlink()


class TestPublisherDests(unittest.TestCase):
    """出版商书签式 named dest（Elsevier bib/fig/tbl/eqn、RSC bm_*）映射。"""

    def _run(self, page0_text, link_specs, blocks):
        """link_specs: [(源文字, dest_name)]；dest_page=-1 → 同号唯一兜底解析。"""
        doc = fitz.open()
        doc.new_page().insert_text((72, 100), page0_text, fontsize=11)
        fd, path = tempfile.mkstemp(suffix=".pdf")
        os.close(fd)
        doc.save(path)
        doc.close()
        doc = fitz.open(path)
        pages = [le._Page(doc[0])]
        links = []
        for txt, dest in link_specs:
            c0 = pages[0].raw.find(txt)
            self.assertGreaterEqual(c0, 0, f"{txt!r} 不在页文本里")
            links.append(le._Link(0, c0, c0 + len(txt), txt, dest_name=dest))
        orig = le.extract_pdf_links
        le.extract_pdf_links = lambda _pdf: (pages, links)
        try:
            res = collect_paper_links(blocks, path)
        finally:
            le.extract_pdf_links = orig
            doc.close()
        Path(path).unlink()
        return res

    def test_elsevier_bib_fig_tbl_eqn(self):
        text = "See 3 and Fig. 2 and Table 4 and Equation 1 here."
        blocks = [
            ProcessedBlock("paragraph", content=text, src_page=0),
            ProcessedBlock("reference", content="[3] X. Author, T.", src_page=0),
            ProcessedBlock("image", content="Figure 2: A plot.",
                           img_new_name="fig2.jpg", src_page=0),
            ProcessedBlock("table_image", content="Table 4: Data.",
                           img_new_name="table1.png", src_page=0),
            ProcessedBlock("equation", content="$$\ny = x \\tag{1}\n$$", src_page=0),
        ]
        res = self._run(text, [("3", "bib0003"), ("Fig. 2", "fig0002"),
                               ("Table 4", "tbl4"), ("Equation 1", "eqn0001")],
                        blocks)
        self.assertEqual(
            blocks[0].content,
            "See [3](#ref-3) and [Fig. 2](#fig-2) and [Table 4](#tab-4) "
            "and [Equation 1](#eq-1) here.")
        self.assertEqual(res.stats.get("ref"), [1, 0])
        self.assertEqual(res.stats.get("fig"), [1, 0])
        self.assertEqual(res.stats.get("tab"), [1, 0])
        self.assertEqual(res.stats.get("eq"), [1, 0])

    def test_prefixed_uppercase_dest(self):
        # ernst 风格 MAC11592BIB59：前缀大写词干同样识别
        text = "Earlier work [7]. reports."
        blocks = [
            ProcessedBlock("paragraph", content=text, src_page=0),
            ProcessedBlock("reference", content="[7] Y. Author, U.", src_page=0),
        ]
        res = self._run(text, [("[7].", "MAC11592BIB7")], blocks)
        self.assertEqual(blocks[0].content,
                         "Earlier work [[7].](#ref-7) reports.")

    def test_dest_text_number_mismatch_dropped(self):
        text = "See 3 here."
        blocks = [
            ProcessedBlock("paragraph", content=text, src_page=0),
            ProcessedBlock("reference", content="[9] X. Author, T.", src_page=0),
        ]
        res = self._run(text, [("3", "bib0009")], blocks)
        self.assertEqual(blocks[0].content, "See 3 here.")
        self.assertEqual(res.stats.get("ref"), [0, 1])

    def test_supplementary_dest_skipped(self):
        text = "Data 1 here."
        blocks = [ProcessedBlock("paragraph", content=text, src_page=0)]
        res = self._run(text, [("1", "bm_MOESM1")], blocks)
        self.assertEqual(blocks[0].content, "Data 1 here.")
        self.assertEqual(res.stats.get("other"), [0, 1])

    def test_superscript_cite_unique_fallback(self):
        # RSC 式上标引文：PDF 是裸数字上标，块内 "[5]" 的括号是引擎归一产物，
        # 字符级对齐必败 → 唯一出现兜底落位
        text = "Grid scale storage 5 is key."
        blocks = [
            ProcessedBlock("paragraph", content="Grid scale storage [5] is key.",
                           src_page=0),
            ProcessedBlock("reference", content="[5] X. Author, T.", src_page=1),
        ]
        res = self._run(text, [("5", "bm_CR5")], blocks)
        self.assertEqual(blocks[0].content,
                         "Grid scale storage [[5]](#ref-5) is key.")
        self.assertEqual(res.stats.get("ref"), [1, 0])

    def test_superscript_cite_ambiguous_dropped(self):
        # 同页两个 [5] 出现位置，落位多义 → 维持放弃
        text = "Storage 5 and later 5 again."
        blocks = [
            ProcessedBlock("paragraph",
                           content="Storage [5] and later [5] again.", src_page=0),
            ProcessedBlock("reference", content="[5] X. Author, T.", src_page=1),
        ]
        res = self._run(text, [("5", "bm_CR5")], blocks)
        self.assertEqual(blocks[0].content, "Storage [5] and later [5] again.")
        self.assertEqual(res.stats.get("ref"), [0, 1])

    def test_superscript_cite_math_tail_recovered(self):
        # RSC 上标引文尾吞数学段（guo2017 实测）：PDF 是裸数字上标，引擎把
        # "SIBs 19, 20" 归一成 $SIBs^{19, 20}$ → 剥出簇并逐号成链
        text = "studied as cathodes for SIBs19, 20, and in particular Mn-based."
        blocks = [
            ProcessedBlock(
                "paragraph",
                content="studied as cathodes for $SIBs^{19, 20}$ , and in "
                        "particular Mn-based.", src_page=0),
            ProcessedBlock("reference", content="[19] X. Author, T.", src_page=1),
            ProcessedBlock("reference", content="[20] Y. Author, U.", src_page=1),
        ]
        res = self._run(text, [("19", "bm_CR19"), ("20", "bm_CR20")], blocks)
        self.assertEqual(
            blocks[0].content,
            "studied as cathodes for $SIBs$[[19](#ref-19), [20](#ref-20)] , "
            "and in particular Mn-based.")
        self.assertEqual(res.stats.get("ref"), [2, 0])

    def test_superscript_cite_whole_span_range_recovered(self):
        # 整段上标引文数学段含区间：$^{11-18}$ → [[11](#ref-11)-[18](#ref-18)]
        text = "layered oxides, polyanion compounds, and Prussian-blue analogs 11-18 ."
        blocks = [
            ProcessedBlock(
                "paragraph",
                content="layered oxides, polyanion compounds, and Prussian-blue "
                        "analogs $^{11-18}$ .", src_page=0),
            ProcessedBlock("reference", content="[11] X. Author, T.", src_page=1),
            ProcessedBlock("reference", content="[18] Y. Author, U.", src_page=1),
        ]
        res = self._run(text, [("11", "bm_CR11"), ("18", "bm_CR18")], blocks)
        self.assertEqual(
            blocks[0].content,
            "layered oxides, polyanion compounds, and Prussian-blue analogs "
            "[[11](#ref-11)-[18](#ref-18)] .")
        self.assertEqual(res.stats.get("ref"), [2, 0])

    def test_superscript_cite_span_incomplete_dropped(self):
        # 簇内 2 的链接缺失/对不上 → 整段放弃（全有或全无，绝不拆一半）
        text = "a smart choice, efficiently advancing reliability 1, 2. The cost"
        blocks = [
            ProcessedBlock(
                "paragraph",
                content="a smart choice, efficiently advancing reliability "
                        "$^{1, 2}$ . The cost", src_page=0),
            ProcessedBlock("reference", content="[1] X. Author, T.", src_page=1),
            ProcessedBlock("reference", content="[2] Y. Author, U.", src_page=1),
        ]
        res = self._run(text, [("1", "bm_CR1")], blocks)
        self.assertEqual(
            blocks[0].content,
            "a smart choice, efficiently advancing reliability $^{1, 2}$ . The cost")
        self.assertEqual(res.stats.get("ref"), [0, 1])

    def test_superscript_cite_bare_tail_split(self):
        # 无 ^ 包裹的裸数字附录形态（forecast 脚注病例的引文版）：
        # $f^{-4/3}2$ → $f^{-4/3}$[[2]](#ref-2)
        text = "modes decrease in proportion to f^{-4/3}2 after the burst."
        blocks = [
            ProcessedBlock(
                "paragraph",
                content="modes decrease in proportion to $f^{-4/32}$ after "
                        "the burst.", src_page=0),
            ProcessedBlock("reference", content="[2] X. Author, T.", src_page=1),
        ]
        res = self._run(text, [("2", "bm_CR2")], blocks)
        self.assertEqual(
            blocks[0].content,
            "modes decrease in proportion to $f^{-4/3}$[[2]](#ref-2) after "
            "the burst.")
        self.assertEqual(res.stats.get("ref"), [1, 0])

    def test_isotope_span_not_citation_dropped(self):
        # 同位素 $^{14}N$：段芯不以编号收尾，形态判据拒绝——即便链接落在
        # 段内也绝不拆（防误伤真数学）
        text = "the isotope 14N decays quickly."
        blocks = [
            ProcessedBlock("paragraph", content="the isotope $^{14}N$ decays "
                                               "quickly.", src_page=0),
            ProcessedBlock("reference", content="[14] X. Author, T.", src_page=1),
        ]
        res = self._run(text, [("14", "bm_CR14")], blocks)
        self.assertEqual(blocks[0].content,
                         "the isotope $^{14}N$ decays quickly.")
        self.assertEqual(res.stats.get("ref"), [0, 1])

    def test_parse_dest_string(self):
        self.assertEqual(le._parse_dest_string("/FitR 0 446 596 437", 800.0),
                         (298.0, 358.5))
        self.assertEqual(le._parse_dest_string("/XYZ 32 748 0", 800.0),
                         (32.0, 52.0))
        self.assertEqual(le._parse_dest_string("/FitH 100", 800.0), (0.0, 700.0))
        self.assertIsNone(le._parse_dest_string("/Fit", 800.0))
        self.assertIsNone(le._parse_dest_string("", 800.0))


class TestRenderAnchors(unittest.TestCase):
    def test_anchor_emission(self):
        blocks = [
            ProcessedBlock("heading", content="II. Methods", level=1),
            ProcessedBlock("paragraph", content="Body text."),
            ProcessedBlock("heading", content="References", level=1),
            ProcessedBlock("reference",
                           content="[12] X. Author, Great Title."),
            ProcessedBlock("image", content="Figure 3: A sample plot.",
                           img_new_name="fig3.jpg"),
            ProcessedBlock("equation", content="$$\nE = mc^2 \\tag{5}\n$$"),
        ]
        with tempfile.TemporaryDirectory() as td:
            md = render_paper(
                blocks=blocks,
                metadata={"title": "T", "author": [{"name": "A"}], "date": "2024"},
                output_dir=Path(td), slug="t",
                link_anchors={"ref-12", "fig-3", "sec-ii-methods", "sec-absent",
                              "eq-5"},
            )
            text = md.read_text(encoding="utf-8")
        self.assertIn('<a id="sec-ii-methods"></a>\n# II. Methods', text)
        self.assertIn('<a id="ref-12"></a>[12] X. Author, Great Title.', text)
        self.assertIn('<a id="fig-3"></a>\n![Figure 3](images/fig3.jpg)', text)
        self.assertIn('<a id="eq-5"></a>\n$$\nE = mc^2 \\tag{5}\n$$', text)
        # 不在集合内的块（References 标题）不发射锚点
        self.assertNotIn("sec-references", text)

    def test_no_anchors_by_default(self):
        blocks = [ProcessedBlock("reference", content="[12] X. Author.")]
        with tempfile.TemporaryDirectory() as td:
            md = render_paper(
                blocks=blocks,
                metadata={"title": "T", "author": [{"name": "A"}], "date": "2024"},
                output_dir=Path(td), slug="t",
            )
            self.assertNotIn("<a id=", md.read_text(encoding="utf-8"))

    def test_all_ref_anchors_emitted(self):
        # 任务1：锚点集合含全部条目编号时逐条发射（不依赖链接驱动），
        # 与链接驱动锚点去重；同号条目只在首条发射
        blocks = [
            ProcessedBlock("heading", content="References", level=1),
            ProcessedBlock("reference", content="[1] A. Author, T1."),
            ProcessedBlock("reference", content="[2] B. Author, T2."),
            ProcessedBlock("reference", content="[3] C. Author, T3."),
        ]
        with tempfile.TemporaryDirectory() as td:
            md = render_paper(
                blocks=blocks,
                metadata={"title": "T", "author": [{"name": "A"}], "date": "2024"},
                output_dir=Path(td), slug="t",
                link_anchors={"ref-1", "ref-2", "ref-3"},
            )
            text = md.read_text(encoding="utf-8")
        self.assertIn('<a id="ref-1"></a>[1] A. Author', text)
        self.assertIn('<a id="ref-2"></a>[2] B. Author', text)
        self.assertIn('<a id="ref-3"></a>[3] C. Author', text)
        self.assertEqual(text.count('<a id="ref-1"></a>'), 1)


if __name__ == "__main__":
    unittest.main()
