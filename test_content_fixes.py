"""content_processor 修复单测：多图注粘连拆分（任务2）+ 文献区混入正文段重定位（任务3）。"""

import os
import tempfile
import unittest
from pathlib import Path

import fitz

from content_processor import (
    ProcessedBlock,
    _assign_figure_numbers,
    _relocate_stray_reference_paragraphs,
    _split_glued_caption,
)


def _img(name: str, caption: str = "", page: int = 10) -> ProcessedBlock:
    return ProcessedBlock("image", content=caption, caption=caption,
                          img_src=f"images/{name}.jpg", page_idx=page)


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


if __name__ == "__main__":
    unittest.main()
