"""qc_paper 图表断号检查的外部归属误报修复单测。

原形：forecast 论文 "…following the procedure given in Ref. [55], whose
Table 2 or Table 3 provides …"——引用别论文的表，该文自身零表注，
修复前误报"缺 Table 2/3"（且进严重级引导换引擎重解析，纯属误报）。
"""

import unittest

from qc_paper import _check_fig_table_continuity, _internal_fig_table_refs


class TestExternalAttribution(unittest.TestCase):
    def test_whose_clause_excluded(self):
        prose = ("We calculate the overlap reduction function following the "
                 "procedure given in Ref. [55], whose Table 2 or Table 3 "
                 "provides the relative positions of future detectors.")
        figs, tbls = _internal_fig_table_refs(prose)
        self.assertEqual(tbls, set())

    def test_whose_clause_link_syntax(self):
        # 带链接形态：Ref. [[55](#ref-55)], whose Table 2
        prose = ("the procedure given in Ref. [[55](#ref-55)], whose "
                 "Table 2 or Table 3 provides the positions.")
        figs, tbls = _internal_fig_table_refs(prose)
        self.assertEqual(tbls, set())

    def test_of_in_ref_excluded(self):
        for s in ("see Table 2 of Ref. [55] for details",
                  "see Table 2 of [55] for details",
                  "as shown in Table 2 in Ref. [55], x",
                  "as shown in Table 2 in [55], x"):
            figs, tbls = _internal_fig_table_refs(s)
            self.assertEqual(tbls, set(), s)

    def test_ref_prefix_excluded(self):
        figs, tbls = _internal_fig_table_refs("Ref. [55] Table 2 lists them.")
        self.assertEqual(tbls, set())

    def test_figure_same_rule(self):
        figs, tbls = _internal_fig_table_refs(
            "following Ref. [7], whose Figure 3 shows the spectrum.")
        self.assertEqual(figs, set())

    def test_internal_mention_kept(self):
        # 真阳性：内部引用照常计入
        figs, tbls = _internal_fig_table_refs(
            "Table 2 shows the results; Fig. 4 plots them.")
        self.assertEqual(tbls, {2})
        self.assertEqual(figs, {4})

    def test_internal_after_external_kept(self):
        # 外部提及之后的内部提及不受连坐："or Table 3" 被判外部后，
        # 新句的 "Our Table 5 summarizes" 仍是内部引用
        prose = ("given in Ref. [55], whose Table 2 or Table 3 provides positions. "
                 "Our Table 5 summarizes our own results.")
        figs, tbls = _internal_fig_table_refs(prose)
        self.assertEqual(tbls, {5})


class TestContinuityEndToEnd(unittest.TestCase):
    def test_zero_tables_all_external_no_warn(self):
        body = ("# Introduction\n\nWe follow the procedure given in Ref. [55], "
                "whose Table 2 or Table 3 provides the positions.\n\n"
                "# References\n\n[55] X. Author, T.\n")
        self.assertEqual(_check_fig_table_continuity(body), [])

    def test_zero_tables_internal_mention_warns(self):
        # 真阳性不丢：确无表格且正文内部引用 Table 2 → 仍告警
        body = ("# Introduction\n\nTable 2 shows our results clearly.\n\n"
                "# References\n\n[1] X. Author, T.\n")
        warns = _check_fig_table_continuity(body)
        self.assertTrue(any("缺 Table 2" in w for w in warns), warns)

    def test_figure_external_no_warn(self):
        body = ("# Results\n\nWe compare with Ref. [9], whose Figure 2 "
                "gives the spectrum.\n")
        self.assertEqual(_check_fig_table_continuity(body), [])


if __name__ == "__main__":
    unittest.main()
