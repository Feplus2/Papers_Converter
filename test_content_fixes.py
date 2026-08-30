"""content_processor 修复单测：多图注粘连拆分（任务2）+ 文献区混入正文段重定位（任务3）。"""

import os
import tempfile
import unittest
from pathlib import Path

import fitz

from content_processor import (
    ProcessedBlock,
    _assign_figure_numbers,
    _extract_caption,
    _parse_footnote_num,
    _relocate_stray_reference_paragraphs,
    _split_glued_caption,
    process_content,
)


def _img(name: str, caption: str = "", page: int = 10,
         bbox=None) -> ProcessedBlock:
    return ProcessedBlock("image", content=caption, caption=caption,
                          img_src=f"images/{name}.jpg", page_idx=page,
                          bbox=bbox if bbox is not None else [100, 100, 400, 300])


class TestGluedCaptionSplit(unittest.TestCase):
    CAP5 = ("The mass and momentum spectrum of loops in the radiation era, "
            "using simulation data for the loop production function.")
    CAP6 = ("The momentum distribution of loops for a slice with constant "
            "alpha during the radiation era.")

    def test_split_two_figures(self):
        # blanco2024 实证形态：两图同页，双图注粘连在靠后图块
        blocks = [
            ProcessedBlock("paragraph", content="Body text about loops."),
            _img("a"),
            _img("b", caption=f"FIG. 5. {self.CAP5} FIG. 6. {self.CAP6}"),
        ]
        _assign_figure_numbers(blocks)
        a, b = blocks[1], blocks[2]
        self.assertEqual(a.img_new_name, "fig5.jpg")
        self.assertEqual(b.img_new_name, "fig6.jpg")
        self.assertTrue(a.content.startswith("Figure 5: The mass"))
        self.assertTrue(b.content.startswith("Figure 6: The momentum"))
        self.assertNotIn("FIG. 6", a.content)

    def test_no_split_when_counts_mismatch(self):
        # 3 个标记但只有 2 张图 → 不拆（保守维持原样）
        cap = f"FIG. 5. {self.CAP5} FIG. 6. {self.CAP6} FIG. 7. {self.CAP5}"
        blocks = [ProcessedBlock("paragraph", content="Body text."),
                  _img("a"), _img("b", caption=cap)]
        _assign_figure_numbers(blocks)
        self.assertEqual(blocks[2].img_new_name, "fig5.jpg")
        self.assertEqual(blocks[1].img_new_name, "fig5a.jpg")  # 仍按子图处理

    def test_panel_letter_not_split(self):
        # "Fig. 3a" 面板字母与 "Fig. 7)." 句中引用都不是新图注起点
        self.assertEqual(_split_glued_caption(
            "Figure 3: Comparison of panels; see Fig. 3a and Fig. 7). "
            "A sufficiently long caption text here."), [])

    def test_single_marker_untouched(self):
        self.assertEqual(_split_glued_caption(f"FIG. 5. {self.CAP5}"), [])


class TestReferenceRelocation(unittest.TestCase):
    BODY = ("We have also thoroughly tested the validity of this approximation "
            "for the range of parameters considered in the present work, "
            "finding agreement in all cases examined.")

    def _blocks(self):
        return [
            ProcessedBlock("paragraph", content="Last body paragraph ends here."),
            ProcessedBlock("heading", content="References"),
            ProcessedBlock("paragraph", content=self.BODY),   # 混入的正文段
            ProcessedBlock("reference", content="[1] A. Author, T., 2020."),
            ProcessedBlock("reference", content="[2] B. Author, U., 2021."),
        ]

    def test_relocate_stray_paragraph(self):
        blocks = self._blocks()
        out = _relocate_stray_reference_paragraphs(blocks)
        kinds = [(b.kind, (b.content or "")[:20]) for b in out]
        # 正文段搬到 References 标题之前，条目流保持连续
        self.assertEqual(out[1].content, self.BODY)
        self.assertEqual(out[2].content, "References")
        self.assertEqual([k for k, _ in kinds[3:]],
                         ["reference", "reference"])

    def test_no_relocate_without_following_entry(self):
        # 段落在文献序列末尾（后邻不是条目）→ 不动
        blocks = [
            ProcessedBlock("heading", content="References"),
            ProcessedBlock("reference", content="[1] A. Author, T., 2020."),
            ProcessedBlock("paragraph", content=self.BODY),
        ]
        out = _relocate_stray_reference_paragraphs(blocks)
        self.assertEqual(out[-1].content, self.BODY)

    def test_apa_unnumbered_entries_untouched(self):
        # APA 无编号条目流是 reference 块——重定位只动 paragraph，零误伤
        blocks = [
            ProcessedBlock("heading", content="References"),
            ProcessedBlock("reference",
                           content="Abramovich, G. N. (1963). The theory of turbulent jets."),
            ProcessedBlock("reference",
                           content="Bejan, A. (1984). Convection heat transfer. Wiley."),
        ]
        out = _relocate_stray_reference_paragraphs(blocks)
        self.assertEqual([b.kind for b in out],
                         ["heading", "reference", "reference"])
        self.assertEqual(out[1].content, blocks[1].content)

    def test_short_fragment_stays(self):
        # 短碎片（<100 字符）不满足正文句形态 → 不搬
        blocks = [
            ProcessedBlock("heading", content="References"),
            ProcessedBlock("paragraph", content="Short note."),
            ProcessedBlock("reference", content="[1] A. Author, T., 2020."),
        ]
        out = _relocate_stray_reference_paragraphs(blocks)
        self.assertEqual(out[1].content, "Short note.")


class TestFigureMergeGuards(unittest.TestCase):
    """figure_merger 编号守卫 + pipeline 坐标空间嗅探（blanco 劣化事故根修）。"""

    def _two_fig_blocks(self, cap5, cap6):
        b1 = ProcessedBlock("image", content=cap5, caption=cap5,
                            img_src="images/a.jpg", img_new_name="fig5.jpg",
                            page_idx=0, bbox=[100, 100, 900, 480])
        b2 = ProcessedBlock("image", content=cap6, caption=cap6,
                            img_src="images/b.jpg", img_new_name="fig6.jpg",
                            page_idx=0, bbox=[150, 620, 850, 950])
        return [b1, b2]

    def _blank_pdf(self, td):
        doc = fitz.open()
        doc.new_page(width=612, height=792)
        path = Path(td) / "t.pdf"
        doc.save(path)
        doc.close()
        return path

    def test_distinct_figure_numbers_never_merge(self):
        # 同页两个不同编号的独立图：任何坐标空间（含错配的 paddleocr）都不得并。
        # 用 blanco 事故原形：content 带 "Figure N:" 前缀但 caption 是裸图注
        # （_REAL_CAPTION_RE 组界判定读 caption 优先，裸图注不命中 → 会成组，
        # 此时只有编号守卫拦得住）
        import figure_merger
        with tempfile.TemporaryDirectory() as td:
            pdf = self._blank_pdf(td)
            b1, b2 = self._two_fig_blocks("Figure 5: " + "x" * 60,
                                          "Figure 6: " + "y" * 60)
            b1.caption = "The mass and momentum spectrum " + "x" * 40
            b2.caption = "The momentum distribution " + "y" * 40
            n = figure_merger.merge_split_figures(
                [b1, b2], pdf, Path(td), coord_space="paddleocr")
            self.assertEqual(n, 0)
            self.assertEqual(len([b1, b2]), 2)

    def test_same_number_panels_still_merge(self):
        # 同号子图（Figure 5 / Figure 5 (a)）编号守卫放行，照常整幅重裁
        import figure_merger
        with tempfile.TemporaryDirectory() as td:
            pdf = self._blank_pdf(td)
            blocks = self._two_fig_blocks("Figure 5 (a)", "Figure 5: " + "y" * 60)
            blocks[0].img_new_name = "fig5a.jpg"
            blocks[1].img_new_name = "fig5.jpg"
            n = figure_merger.merge_split_figures(
                blocks, pdf, Path(td), coord_space="paddleocr")
            self.assertEqual(n, 1)
            survivors = [b for b in blocks if b.kind == "image"]
            self.assertEqual(len(survivors), 1)
            self.assertIn("_merged", survivors[0].img_src)

    def test_sniff_coord_space(self):
        import json
        from pipeline import _sniff_coord_space
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "st"
            d.mkdir()
            (d / "x_content_list.json").write_text(json.dumps(
                [{"type": "text", "text": "t", "bbox": [90, 144, 366, 880]}]),
                encoding="utf-8")
            self.assertEqual(_sniff_coord_space(d), "mineru")
            (d / "x_content_list.json").write_text(json.dumps(
                [{"type": "text", "text": "t", "bbox": [180, 288, 1224, 1584]}]),
                encoding="utf-8")
            self.assertEqual(_sniff_coord_space(d), "paddleocr")
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(_sniff_coord_space(Path(td)))


class TestCaptionPanelReorder(unittest.TestCase):
    """forecast 事故形态：chart_caption 列表面板标签行在真图注行之前。"""

    def test_panel_labels_after_real_caption(self):
        block = {"chart_caption": [
            "(c) For $G \\mu = 1 0 ^ { - 7 }$ and various $p .",
            "Figure 3: The burst rate $d R / d$ ln h in terms of $f h$ "
            "for various parameter sets.",
        ]}
        cap = _extract_caption(block)
        self.assertTrue(cap.startswith("Figure 3: The burst rate"))
        self.assertIn("(c) For", cap)  # 面板标签保留在真图注之后，零丢失

    def test_single_caption_line_untouched(self):
        # blanco 粘连形态（单个字符串含两个图注标记）不被重排，交下游拆分
        glued = ("FIG. 5. The mass and momentum spectrum of loops in the "
                 "radiation era with a long enough description. FIG. 6. "
                 "The momentum distribution of loops for a slice with "
                 "constant alpha during the radiation era.")
        self.assertEqual(_extract_caption({"chart_caption": [glued]}), glued)

    def test_end_to_end_main_caption(self):
        # 全链路：面板乱序图注 → 主图块 content 以 "Figure N: 真图注" 起首
        cl = [
            {"type": "text", "text": "Body text about the forecast results.",
             "page_idx": 0},
            {"type": "chart", "page_idx": 0, "img_path": "images/x.jpg",
             "bbox": [100, 100, 900, 480],
             "chart_caption": ["(c) $p = 1 0 ^ { - 2 }$",
                               "Figure 5: The parameter regions excluded by "
                               "current experiments and constraints."]},
        ]
        blocks = process_content(cl, "", use_llm=False, title="Forecast test")
        imgs = [b for b in blocks if b.kind == "image"]
        self.assertEqual(len(imgs), 1)
        self.assertTrue(imgs[0].content.startswith(
            "Figure 5: The parameter regions excluded"), imgs[0].content)
        self.assertIn("(c)", imgs[0].content)


class TestFootnotePandoc(unittest.TestCase):
    def test_parse_num_forms(self):
        self.assertEqual(_parse_footnote_num("$^{1}$ For simplicity, we omit"),
                         (1, "For simplicity, we omit"))
        self.assertEqual(_parse_footnote_num("6 Note that, the direction"),
                         (6, "Note that, the direction"))
        self.assertEqual(_parse_footnote_num("4Note that bursts"), (4, "Note that bursts"))
        self.assertEqual(_parse_footnote_num("1GWs from kinks may"), (1, "GWs from kinks may"))
        # 量值/年份不是编号
        self.assertEqual(_parse_footnote_num("2020 was a good year")[0], None)
        self.assertEqual(_parse_footnote_num("3.5 sigma deviation")[0], None)
        self.assertEqual(_parse_footnote_num("\\*skuro@icrr.u-tokyo.ac.jp")[0], None)

    def test_continuation_joined(self):
        cl = [
            {"type": "text", "text": "Body paragraph text here.", "page_idx": 5},
            {"type": "page_footnote", "page_idx": 5,
             "text": "5 Higher signal to noise ratio may be required, since"},
            {"type": "page_footnote", "page_idx": 5,
             "text": "of the amplitude A deviates from the Gaussian shape."},
        ]
        blocks = process_content(cl, "", use_llm=False, title="Fn test")
        fns = [b for b in blocks if b.kind == "footnote"]
        self.assertEqual(len(fns), 1)
        self.assertEqual(fns[0].note_num, 5)
        self.assertIn("Gaussian shape", fns[0].content)

    def test_symbol_footnote_plain(self):
        cl = [
            {"type": "text", "text": "Body paragraph text here.", "page_idx": 0},
            {"type": "page_footnote", "page_idx": 0,
             "text": "\\* Electronic address: sergei@astro.up.pt"},
        ]
        blocks = process_content(cl, "", use_llm=False, title="Fn test")
        fns = [b for b in blocks if b.kind == "footnote"]
        self.assertEqual(len(fns), 1)
        self.assertIsNone(fns[0].note_num)
        self.assertIn("Electronic address", fns[0].content)

    def test_render_pandoc_form(self):
        from renderer import render_paper
        with tempfile.TemporaryDirectory() as td:
            blocks = [ProcessedBlock("paragraph", content="Body text."),
                      ProcessedBlock("footnote", content="Note that, the direction of degeneracy."),
                      ]
            blocks[1].note_num = 6
            md = render_paper(blocks=blocks,
                              metadata={"title": "T", "author": [{"name": "A"}], "date": "2024"},
                              output_dir=Path(td), slug="t")
            text = md.read_text(encoding="utf-8")
        self.assertIn("[^6]: Note that, the direction of degeneracy.", text)


class TestSectionGapRescue(unittest.TestCase):
    """IV 标题被当页眉过滤的断档捞回（martins2000 事故原形）。"""

    def _heads(self, *titles):
        return [ProcessedBlock("heading", content=t, level=1) for t in titles]

    def test_rescue_from_header_pool(self):
        from content_processor import _rescue_missing_section_headings
        blocks = [
            ProcessedBlock("heading", content="III. THE MOMENTUM PARAMETER", level=1),
            ProcessedBlock("paragraph", content="III 节正文收尾段落，内容较长以通过判定。" * 5,
                           src_page=5),
            ProcessedBlock("page_anchor", content="7", page_idx=6),
            # IV 节内容（标题被过滤）：页 6 的首个正文块
            ProcessedBlock("paragraph", content="We now turn to the effect of radiation back-reaction on the evolution of the string network in this section." ,
                           src_page=6),
            ProcessedBlock("heading", content="V. STRING NETWORKS IN GENERAL FRW SPACETIMES", level=1),
        ]
        # 需要 ≥3 个一级罗马标题才判定体系——补 I/II
        blocks = [ProcessedBlock("heading", content="I. INTRODUCTION", level=1),
                  ProcessedBlock("heading", content="II. MODEL", level=1)] + blocks
        dropped = [{"type": "header", "page_idx": 6,
                    "text": "IV. THE EFFECT OF RADIATION BACK-REACTION"}]
        out = _rescue_missing_section_headings(blocks, dropped)
        titles = [(b.kind, b.content) for b in out]
        iv_idx = next(i for i, (k, c) in enumerate(titles)
                      if c.startswith("IV. THE EFFECT"))
        # 标题落在 IV 节内容块之前、页锚之后（不在 V 之前）
        self.assertEqual(titles[iv_idx - 1][0], "page_anchor")
        self.assertEqual(titles[iv_idx + 1][1][:12], "We now turn ")
        self.assertLess(iv_idx,
                        next(i for i, (k, c) in enumerate(titles)
                             if c.startswith("V. STRING")))

    def test_no_candidate_stays(self):
        from content_processor import _rescue_missing_section_headings
        blocks = self._heads("I. A", "II. B", "IV. C")
        out = _rescue_missing_section_headings(blocks, [])
        self.assertEqual(len(out), 3)

    def test_no_gap_untouched(self):
        from content_processor import _rescue_missing_section_headings
        blocks = self._heads("I. A", "II. B", "III. C")
        out = _rescue_missing_section_headings(
            blocks, [{"type": "header", "text": "IV. SOMETHING", "page_idx": 1}])
        self.assertEqual(len(out), 3)

    def test_non_roman_untouched(self):
        from content_processor import _rescue_missing_section_headings
        blocks = self._heads("1 Intro", "3 Methods")  # 点号编号不参与罗马断档
        out = _rescue_missing_section_headings(
            blocks, [{"type": "header", "text": "2. Results", "page_idx": 1}])
        self.assertEqual(len(out), 2)


class TestRefTextListBlocks(unittest.TestCase):
    """list + sub_type:ref_text 块映射（forecast 零丢失事故原形）。"""

    def test_ref_text_list_items_become_references(self):
        cl = [
            {"type": "text", "text": "Body text ends here.", "page_idx": 24,
             "text_level": 1},
            {"type": "list", "sub_type": "ref_text", "page_idx": 24,
             "list_items": ["[1] T. W. B. Kibble, J. Phys. A 9, 1387 (1976).",
                            "[2] A. Vilenkin and E. P. S. Shellard, Cosmic Strings."]},
            {"type": "list", "sub_type": "ref_text", "page_idx": 25,
             "list_items": ["[3] S. Sarangi and S. H. H. Tye, Phys. Lett. B 536."]},
        ]
        blocks = process_content(cl, "", use_llm=False, title="Forecast test")
        refs = [b for b in blocks if b.kind == "reference"]
        self.assertEqual(len(refs), 3)
        self.assertEqual(refs[0].content[:4], "[1] ")
        self.assertEqual(refs[0].src_page, 24)
        self.assertEqual(refs[2].src_page, 25)
        # 条目编号与 split_reference_entries/#ref-N 同源
        import reference_parser as rp
        entries = rp.split_reference_entries(blocks)
        self.assertEqual([e["n"] for e in entries], [1, 2, 3])

    def test_non_ref_list_text_preserved(self):
        # 其他 sub_type（itemize 正文列表）逐条段落保底，不整块丢弃
        cl = [
            {"type": "text", "text": "We list the assumptions.", "page_idx": 0},
            {"type": "list", "sub_type": "itemize", "page_idx": 0,
             "list_items": ["First assumption holds.", "Second assumption fails."]},
        ]
        blocks = process_content(cl, "", use_llm=False, title="List test")
        texts = [b.content for b in blocks if b.kind == "paragraph"]
        self.assertIn("First assumption holds.", texts)
        self.assertIn("Second assumption fails.", texts)


class TestMergerCaptionAndConsolidation(unittest.TestCase):
    """A/B 修复：主图注 caption 前缀形态 + 同图碎片跨 run 归并。"""

    def _pdf(self, td):
        doc = fitz.open()
        doc.new_page(width=612, height=792)
        path = Path(td) / "t.pdf"
        doc.save(path)
        doc.close()
        return path

    def test_main_caption_prefixed_for_merger(self):
        # 主图注块 caption 必须是 "Figure N: ..." 形态——否则 merger 的
        # 真图注组界判定认不出主块，整组合并时图注随被丢块消失
        blocks = [
            ProcessedBlock("paragraph", content="Body text before figures."),
            _img("p1", page=4),
            _img("main", caption="Fig. 3 (a) Comparison of the abundance of "
                                 "the resources of Na and Li. (b) Distribution map.",
                 page=4),
        ]
        _assign_figure_numbers(blocks)
        main = blocks[2]
        self.assertEqual(main.img_new_name, "fig3.jpg")
        self.assertTrue(main.caption.startswith("Figure 3:"), main.caption[:30])

    def test_same_stem_runs_consolidated(self):
        # 同页同图编号的两个 run（中间隔长文字块）归并为一次整幅重裁
        import figure_merger
        with tempfile.TemporaryDirectory() as td:
            pdf = self._pdf(td)
            blocks = [
                _img("a", page=0, bbox=[100, 100, 400, 300]),
                _img("b", page=0, bbox=[420, 100, 700, 300]),
                ProcessedBlock("paragraph", content="x" * 60, src_page=0),
                _img("e", page=0, bbox=[100, 320, 400, 500]),
                _img("m", caption="Fig. 33 (a) PDF fitting curves with a "
                                 "sufficiently long real caption here.", page=0,
                     bbox=[420, 320, 700, 500]),
            ]
            _assign_figure_numbers(blocks)
            n = figure_merger.merge_split_figures(
                blocks, pdf, Path(td), coord_space="mineru")
            self.assertEqual(n, 1)
            survivors = [b for b in blocks if b.kind == "image"]
            self.assertEqual(len(survivors), 1)
            self.assertTrue(survivors[0].content.startswith("Figure 33:"))

    def test_different_numbers_not_consolidated(self):
        # 不同图编号同页相邻：归并防线不动（blanco/madler 教训）
        import figure_merger
        with tempfile.TemporaryDirectory() as td:
            pdf = self._pdf(td)
            blocks = [
                _img("a", caption="Fig. 5. " + "First figure caption. " * 3,
                     page=0, bbox=[100, 100, 400, 380]),
                _img("b", caption="Fig. 6. " + "Second figure caption. " * 3,
                     page=0, bbox=[100, 400, 400, 700]),
            ]
            _assign_figure_numbers(blocks)
            figure_merger.merge_split_figures(
                blocks, pdf, Path(td), coord_space="paddleocr")
            self.assertEqual(len([b for b in blocks if b.kind == "image"]), 2)


class TestH3HeadingSplit(unittest.TestCase):
    """C 修复：x.y.z 三级标题与正文粘连块的拆分（RSC 综述形态）。"""

    def test_h3_split(self):
        cl = [
            {"type": "text", "text": "6.2 Strategies.", "page_idx": 44,
             "text_level": 2},
            {"type": "text", "page_idx": 45,
             "text": "6.2.1 Observation of O redox process. Compared to the "
                    "numerous ARR cases in LIBs, there are not sufficient "
                    "pure prototypes for a comprehensive investigation."},
        ]
        blocks = process_content(cl, "", use_llm=False, title="Routes test")
        heads = [b for b in blocks if b.kind == "heading"]
        self.assertTrue(any(h.content.startswith("6.2.1 Observation of O redox")
                            and h.level == 3 for h in heads),
                        [(h.content[:30], h.level) for h in heads])
        paras = [b for b in blocks if b.kind == "paragraph"]
        self.assertTrue(any(p.content.startswith("Compared to the numerous")
                            for p in paras))

    def test_no_false_split(self):
        # 版本号/小数形态不误拆：无"标题句. 正文"结构不命中
        cl = [{"type": "text", "page_idx": 0,
               "text": "In version 3.2.1 we fixed the bug and it was "
                       "documented extensively in the changelog for users."}]
        blocks = process_content(cl, "", use_llm=False, title="T")
        self.assertFalse(any(b.kind == "heading" and "3.2.1" in b.content
                             for b in blocks))

    def test_h3_split_math_body(self):
        # wang2024routes 实测 5.6.1：正文以行内公式 $Na_{x}...$ 起首，
        # 旧判定要求大写/( 起首导致漏拆，TOC 缺 5.6.1
        cl = [
            {"type": "text", "text": "5.6 Crystal parameter modulation.",
             "page_idx": 31, "text_level": 2},
            {"type": "text", "page_idx": 31,
             "text": "5.6.1 Honeycomb ordered superlattice structure. "
                    "$Na_{x}[M1,M2]O_{2}$ layered oxides have gained "
                    "significant popularity due to the binary potential "
                    "properties of two different transition metals."},
        ]
        blocks = process_content(cl, "", use_llm=False, title="Routes test")
        heads = [b for b in blocks if b.kind == "heading"]
        self.assertTrue(
            any(h.content == "5.6.1 Honeycomb ordered superlattice structure"
                and h.level == 3 for h in heads),
            [(h.content[:40], h.level) for h in heads])
        paras = [b for b in blocks if b.kind == "paragraph"]
        self.assertTrue(any(p.content.startswith("$Na_{x}[M1,M2]O_{2}$ layered")
                            for p in paras))

    def test_h3_heading_math_plaintext(self):
        # heading 文本进 TOC/锚点须为纯文本：$^{+}$ 归一为 unicode 上标 ⁺，
        # 不留 "$"/"^{}" LaTeX 残留（wang2024routes 5.6.3 实测）
        cl = [{"type": "text", "page_idx": 40,
               "text": "5.6.3 Na $^{+}$ sites occupation. Besides the "
                      "traditional substitution of transition metal atoms, "
                      "the introduction of foreign atoms in the alkali sites "
                      "is also a feasible way."}]
        blocks = process_content(cl, "", use_llm=False, title="Routes test")
        heads = [b for b in blocks if b.kind == "heading"]
        self.assertEqual(len(heads), 1)
        self.assertEqual(heads[0].content, "5.6.3 Na ⁺ sites occupation")
        self.assertNotIn("$", heads[0].content)
        self.assertNotIn("^{", heads[0].content)

    def test_heading_complex_math_kept(self):
        # 无法完全归约的公式段（含未识别命令）保持 $...$ 原样，不出垃圾文本
        from content_processor import _heading_plain_math
        src = r"3.1 The $O3 \leftrightarrow P3$ transition"
        self.assertEqual(_heading_plain_math(src), src)
        # \mathrm 剥壳 + 下标 unicode 化
        self.assertEqual(_heading_plain_math(r"$\mathrm{Na}$ sites"),
                         "Na sites")
        self.assertEqual(_heading_plain_math(r"$_{2}$ layer"), "₂ layer")


class TestMathBraceRepair(unittest.TestCase):
    """D 修复：数学段花括号多开失衡的尾补 }。"""

    def test_unclosed_group_repaired(self):
        from content_processor import _balance_math_braces
        bad = (r"${\mathrm{{Na}}}_{0.7}{\left\lbrack {\mathrm{Fe}}_{0.2}"
               r"\square _{0.2}\right\rbrack {\mathrm{O}}_{2}$")
        fixed = _balance_math_braces(bad)
        self.assertTrue(fixed.endswith("}}$"), fixed[-10:])

    def test_balanced_untouched(self):
        from content_processor import _balance_math_braces
        good = r"$E = mc^{2}$ and $x_{1}$"
        self.assertEqual(_balance_math_braces(good), good)

    def test_over_closed_untouched(self):
        from content_processor import _balance_math_braces
        odd = r"$x}_{2}$"  # 多闭方向不修
        self.assertEqual(_balance_math_braces(odd), odd)


if __name__ == "__main__":
    unittest.main()
