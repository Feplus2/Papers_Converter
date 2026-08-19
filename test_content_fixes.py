"""content_processor 修复单测：多图注粘连拆分（任务2）+ 文献区混入正文段重定位（任务3）。"""

import unittest

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


if __name__ == "__main__":
    unittest.main()
