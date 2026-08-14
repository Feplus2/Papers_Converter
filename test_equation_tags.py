"""公式 \tag 去重单测（MinerU VLM 编号拆行伪影）。

运行：
    python -m unittest test_equation_tags -v

真实样本来自 cosmic strings 论文（arXiv 2605.22944）Eq.(50) / Eq.(A2)：
两行式 array 公式的编号 "(50)" 被竖排拆开，VLM 先生成残缺 \tag{5} /
\tag{A}，末尾又补完整 \tag{50} / \tag{A2}，双 \tag 被 KaTeX 拒收
（"Multiple \tag"）。
"""

import unittest

from content_processor import _dedup_equation_tags


class TestDedupEquationTags(unittest.TestCase):

    def test_no_tag_untouched(self):
        self.assertEqual(_dedup_equation_tags(r"\alpha = 1"), r"\alpha = 1")

    def test_single_tag_untouched(self):
        self.assertEqual(_dedup_equation_tags(r"\alpha = 1\tag{50}"),
                         r"\alpha = 1\tag{50}")

    def test_eq50_real_artifact(self):
        # 实录形态：残缺编号在前（带空格 \tag {5}），完整编号在后
        text = r"\end{array} \tag {5}\tag{50}"
        self.assertEqual(_dedup_equation_tags(text), r"\end{array} \tag{50}")

    def test_eqA2_real_artifact(self):
        text = r"\end{array} \tag {A}\tag{A2}"
        self.assertEqual(_dedup_equation_tags(text), r"\end{array} \tag{A2}")

    def test_reverse_order_keeps_longest(self):
        # 倒序残缺：取最长而非最末
        self.assertEqual(_dedup_equation_tags(r"\tag{50}\tag{5}"), r"\tag{50}")

    def test_keeps_surrounding_text(self):
        text = r"a\tag{5}b\tag{50}c"
        self.assertEqual(_dedup_equation_tags(text), r"ab\tag{50}c")

    def test_three_tags(self):
        self.assertEqual(_dedup_equation_tags(r"\tag{1}\tag{5}\tag{50}"),
                         r"\tag{50}")

    def test_tie_keeps_last(self):
        # 等长并列取最末
        self.assertEqual(_dedup_equation_tags(r"\tag{5}\tag{4}"), r"\tag{4}")


if __name__ == "__main__":
    unittest.main()
