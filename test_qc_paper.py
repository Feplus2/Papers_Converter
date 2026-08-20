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


class TestHeadingGapWarn(unittest.TestCase):
    def test_gap_warns(self):
        from qc_paper import _check_heading_number_gaps
        body = "# I. INTRODUCTION\n\n# II. MODEL\n\n# III. PARAM\n\n# V. NETWORKS\n"
        w = _check_heading_number_gaps(body)
        self.assertTrue(any("IV" in x for x in w), w)

    def test_complete_quiet(self):
        from qc_paper import _check_heading_number_gaps
        body = "# I. A\n\n# II. B\n\n# III. C\n"
        self.assertEqual(_check_heading_number_gaps(body), [])

    def test_too_few_headings_quiet(self):
        from qc_paper import _check_heading_number_gaps
        self.assertEqual(_check_heading_number_gaps("# I. A\n\n# III. C\n"), [])


class TestEmptyReferencesSevere(unittest.TestCase):
    """forecast 事故：References 标题存在但条目零 → 严重级发现。"""

    def test_empty_references_is_severe(self):
        import tempfile
        from pathlib import Path
        from qc_paper import qc_severe_findings, qc_paper_md
        body = ("---\ntitle: T\n---\n# Intro\n\nBody text.\n\n# References\n")
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "paper.md"
            p.write_text(body, encoding="utf-8")
            severe = qc_severe_findings(p, 5)
        self.assertTrue(any("参考文献区为空" in s for s in severe), severe)

    def test_nonempty_references_quiet(self):
        import tempfile
        from pathlib import Path
        from qc_paper import qc_severe_findings
        body = ("---\ntitle: T\n---\n# Intro\n\nBody.\n\n# References\n\n"
                "<a id=\"ref-1\"></a>[1] X. Author, T., 2020.\n\n"
                "[2] Y. Author, U., 2021.\n")
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "paper.md"
            p.write_text(body, encoding="utf-8")
            severe = qc_severe_findings(p, 5)
        self.assertFalse(any("参考文献区为空" in s for s in severe), severe)

    def test_apa_unnumbered_references_quiet(self):
        # APA 无编号条目流（madler 形态）不命中空文献区
        import tempfile
        from pathlib import Path
        from qc_paper import qc_severe_findings
        body = ("---\ntitle: T\n---\n# Intro\n\nBody.\n\n# References\n\n"
                "Abramovich, G. N. (1963). The theory of turbulent jets.\n")
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "paper.md"
            p.write_text(body, encoding="utf-8")
            severe = qc_severe_findings(p, 5)
        self.assertFalse(any("参考文献区为空" in s for s in severe), severe)


class TestEquationIntegrity(unittest.TestCase):
    """公式结构完整性检查（forecast eq57 截断事故原形；只报近乎确定损坏）。"""

    def _run(self, eq: str):
        from qc_paper import _check_equation_integrity
        return _check_equation_integrity(f"$$\n{eq}\n$$\n")

    def test_frac_truncated_fires(self):
        # forecast eq57 原形：\frac 第二参数落在 \end 上
        w = self._run(r"\begin{array}{l} x = ( \frac \Omega \end{array}\tag{57}")
        self.assertTrue(any("\\frac" in s and "57" in s for s in w), w)

    def test_frac_at_block_end_fires(self):
        w = self._run(r"x = \frac")
        self.assertTrue(any("\\frac" in s for s in w), w)

    def test_legal_frac_forms_quiet(self):
        for eq in (r"\frac{1}{2}", r"\frac12", r"\frac\Omega\chi",
                   r"\dfrac{a}{b} + \binom{n}{k}"):
            self.assertEqual(self._run(eq), [], eq)

    def test_brace_imbalance_fires(self):
        w = self._run(r"x = {a + b \tag{1}")
        self.assertTrue(any("花括号" in s for s in w), w)

    def test_escaped_braces_not_counted(self):
        self.assertEqual(self._run(r"x = \{ a \}"), [])

    def test_begin_end_mismatch_fires(self):
        w = self._run(r"\begin{array}{l} x \end{split}")
        self.assertTrue(any("begin" in s for s in w), w)

    def test_left_right_forms(self):
        self.assertTrue(any("left" in s for s in self._run(r"\left( x \tag{1}")),)
        self.assertEqual(self._run(r"\left\{ x \right."), [])  # 右空定界符合法
        self.assertEqual(self._run(r"\left( x \right)"), [])

    def test_tag_inside_env_fires(self):
        w = self._run(r"\begin{split} x = 1 \tag{3} \\ y = 2 \end{split}")
        self.assertTrue(any("环境内部" in s for s in w), w)

    def test_tag_after_end_quiet(self):
        self.assertEqual(self._run(r"\begin{array}{l} x = 1 \end{array}\tag{3}"), [])

    def test_complete_multibranch_quiet(self):
        # forecast eq56 形态的完整分段公式（\right. 收尾 + \tag 在环境外）
        eq = (r"x = \left\{ \begin{array}{l} a \quad ; h < h_1 \\ "
              r"\frac{b}{c} \quad ; h > h_1 \end{array} \right. ,\tag{56}")
        self.assertEqual(self._run(eq), [])


if __name__ == "__main__":
    unittest.main()
