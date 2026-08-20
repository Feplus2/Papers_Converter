"""P2.1 参考文献条目结构化（reference_parser）单元测试。

覆盖：规则切分（[N]/N. 形态、块内多条目单调递增切分、悬挂缩进续行、
条目内 [5] 防误切、精确去重）、DOI 正则层、LLM 合并/互校/降级（mock）、
端到端 references.json 落盘。
"""

import json
import tempfile
import unittest
from pathlib import Path

import reference_parser as rp
from content_processor import ProcessedBlock


def _ref_blocks(*texts: str) -> list:
    return [ProcessedBlock("reference", content=t) for t in texts]


class TestSplit(unittest.TestCase):
    def test_bracket_entries(self):
        blocks = _ref_blocks("[1] A. Author, Title One, 2020.",
                             "[2] B. Author, Title Two, 2021.")
        entries = rp.split_reference_entries(blocks)
        self.assertEqual([e["n"] for e in entries], [1, 2])
        self.assertTrue(entries[0]["raw"].startswith("[1]"))

    def test_dot_entries(self):
        blocks = _ref_blocks("1. Dunn, B., Science 334, 928 (2011).",
                             "2. Liu, J. et al., Adv. Funct. Mater. 23 (2013).")
        entries = rp.split_reference_entries(blocks)
        self.assertEqual([e["n"] for e in entries], [1, 2])

    def test_bare_number_entries_rsc_style(self):
        # RSC 裸编号形态（wang2024 实测）："1 J. Y. Hwang, ..." 严格连号
        blocks = _ref_blocks(
            "1 J. Y. Hwang, S. T. Myung and Y. K. Sun, Chem. Soc. Rev., 2017, 46, 3529–3614.",
            "2 X. Xiang, K. Zhang and J. Chen, Adv. Mater., 2015, 27, 5343–5364.",
            "3 K. Kubota, N. Yabuuchi et al., Chem. Rev., 2018, 118, 459.")
        entries = rp.split_reference_entries(blocks)
        self.assertEqual([e["n"] for e in entries], [1, 2, 3])

    def test_bare_number_rejects_page_number(self):
        # 裸编号不连号（页码 390 伪起点）→ 不当新条目
        blocks = _ref_blocks(
            "1 A. Author, Title One, 2020, pp. 1–10. 390. VDI Verlag, Düsseldorf.")
        entries = rp.split_reference_entries(blocks)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["n"], 1)

    def test_multi_entry_block_monotonic(self):
        # 单块多条目：按编号标记切；条目 2 正文里的 [5] 不单调递增，不误切
        blocks = _ref_blocks(
            "[1] A. Author, T1. [2] B. Author, citing [5] here, T2. [3] C. Author, T3.")
        entries = rp.split_reference_entries(blocks)
        self.assertEqual([e["n"] for e in entries], [1, 2, 3])
        self.assertIn("citing [5] here", entries[1]["raw"])

    def test_continuation_joined(self):
        blocks = _ref_blocks("[1] A. Author, Very Long Title That",
                             "Wraps Onto Next Block, 2020.")
        entries = rp.split_reference_entries(blocks)
        self.assertEqual(len(entries), 1)
        self.assertIn("Wraps Onto Next Block", entries[0]["raw"])

    def test_unnumbered_first_block(self):
        blocks = _ref_blocks("Author-year style entry without number.")
        entries = rp.split_reference_entries(blocks)
        self.assertEqual(len(entries), 1)
        self.assertIsNone(entries[0]["n"])

    def test_exact_dedup(self):
        blocks = _ref_blocks("[1] A. Author, Same Title, 2020.",
                             "[1] A. Author, Same Title, 2020.")
        entries = rp.split_reference_entries(blocks)
        self.assertEqual(len(entries), 1)

    def test_non_reference_blocks_ignored(self):
        blocks = [ProcessedBlock("paragraph", content="[1] not a reference"),
                  ProcessedBlock("reference", content="[2] Real entry, 2020.")]
        entries = rp.split_reference_entries(blocks)
        self.assertEqual([e["n"] for e in entries], [2])

    def test_unnumbered_author_year_list(self):
        # APA 作者-年份制整条目无编号（madler2001 实测）：每块一条，n=None；
        # 小写起首块判为续行并入上一条
        blocks = _ref_blocks(
            "Abramovich, G. N. (1963). The theory of turbulent jets. M.I.T. Press.",
            "Bejan, A. (1984). Convection heat transfer. New York:",
            " Wiley.",
            "Best, P. E. et al. (1986). Extension of emission technique. Combustion and Flame, 66, 47–66.")
        entries = rp.split_reference_entries(blocks)
        self.assertEqual(len(entries), 3)
        self.assertTrue(all(e["n"] is None for e in entries))
        self.assertTrue(entries[1]["raw"].endswith("Wiley."))

    def test_unnumbered_llm_pairing_by_order(self):
        orig = rp._llm_extract
        # LLM 对无编号条目自作主张补 1..k 编号（madler2001 实测）→ 按位置配对
        rp._llm_extract = lambda e, use_llm: [
            {"n": 1, "title": "Turbulent Jets", "authors": ["G. N. Abramovich"],
             "year": 1963, "venue": "MIT Press", "doi": None},
            {"n": 2, "title": "Convection Heat Transfer", "authors": ["A. Bejan"],
             "year": 1984, "venue": "Wiley", "doi": None}]
        try:
            entries = [
                {"n": None, "raw": "Abramovich, G. N. (1963). The theory of turbulent jets."},
                {"n": None, "raw": "Bejan, A. (1984). Convection heat transfer. Wiley."},
            ]
            refs, source = rp.build_references(entries, use_llm=True)
        finally:
            rp._llm_extract = orig
        self.assertEqual(source, "llm")
        self.assertEqual(refs[0]["title"], "Turbulent Jets")
        self.assertEqual(refs[1]["authors"], ["A. Bejan"])


class TestDoiRegex(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(
            rp.extract_doi("X. Author, Title, Nature 4 (2005) 366, "
                           "https://doi.org/10.1038/nature04236."),
            "10.1038/nature04236")
        self.assertEqual(
            rp.extract_doi("Y. Author, Chem. 8 (2021) 625, DOI: 10.1002/celc.202001383"),
            "10.1002/celc.202001383")
        self.assertEqual(
            rp.extract_doi("Z. Author (2020), doi:10.1016/j.matchemphys.2006.06.014"),
            "10.1016/j.matchemphys.2006.06.014")
        self.assertIsNone(rp.extract_doi("No doi here, just vol 10.1103 without slash suffix"))
        self.assertIsNone(rp.extract_doi("plain entry, no identifier"))

    def test_trailing_paren_balanced(self):
        self.assertEqual(rp.extract_doi("see (10.1016/0550-3213(85)90022-7) end"),
                         "10.1016/0550-3213(85)90022-7")


class TestBuildReferences(unittest.TestCase):
    def setUp(self):
        self.entries = [
            {"n": 1, "raw": "[1] A. Author, Great Paper, Nature 4 (2005) 366, "
                            "https://doi.org/10.1038/nature04236."},
            {"n": 2, "raw": "[2] B. Author, Other Paper, Science 334 (2011) 928."},
        ]
        self._orig = rp._llm_extract

    def tearDown(self):
        rp._llm_extract = self._orig

    def test_llm_merge_and_doi_override(self):
        rp._llm_extract = lambda entries, use_llm: [
            {"n": 1, "title": "Great Paper", "authors": ["A. Author"],
             "year": 2005, "venue": "Nature", "doi": "10.0000/hallucinated"},
            {"n": 2, "title": "Other Paper", "authors": ["B. Author"],
             "year": 2011, "venue": "Science", "doi": None},
        ]
        refs, source = rp.build_references(self.entries, use_llm=True)
        self.assertEqual(source, "llm")
        self.assertEqual(refs[0]["title"], "Great Paper")
        # DOI 冲突以正则为准（LLM 给的幻觉值被覆盖）
        self.assertEqual(refs[0]["doi"], "10.1038/nature04236")
        self.assertEqual(refs[1]["year"], 2011)
        self.assertIsNone(refs[1]["doi"])

    def test_llm_doi_kept_when_regex_silent_and_valid(self):
        # raw 里正则提不到（无 10. 前缀串），LLM 给出形态合法的 doi → 保留
        entries = [{"n": 1, "raw": "[1] A. Author, T., Journal X (2020)."}]
        rp._llm_extract = lambda e, use_llm: [
            {"n": 1, "title": "T.", "authors": [], "year": 2020,
             "venue": "Journal X", "doi": "10.1000/xyz123"}]
        refs, source = rp.build_references(entries, use_llm=True)
        self.assertEqual(refs[0]["doi"], "10.1000/xyz123")
        # LLM 给出形态非法的 doi → 丢弃
        rp._llm_extract = lambda e, use_llm: [
            {"n": 1, "title": "T.", "authors": [], "year": 2020,
             "venue": "Journal X", "doi": "not-a-doi"}]
        refs, _ = rp.build_references(entries, use_llm=True)
        self.assertIsNone(refs[0]["doi"])

    def test_count_mismatch_degrades(self):
        # LLM 只回 1 条（规则切分 2 条，差 50% > 20%）→ 整段降级；
        # 降级 title 置 None（绝不 raw 复读），DOI 正则层仍生效
        rp._llm_extract = lambda e, use_llm: [
            {"n": 1, "title": "Only One", "authors": [], "year": 2005,
             "venue": "Nature", "doi": None}]
        refs, source = rp.build_references(self.entries * 3, use_llm=True)
        self.assertEqual(source, "rule")
        self.assertTrue(all(r["title"] is None for r in refs))
        self.assertEqual(refs[0]["doi"], "10.1038/nature04236")

    def test_llm_failure_degrades(self):
        rp._llm_extract = lambda e, use_llm: None
        refs, source = rp.build_references(self.entries, use_llm=True)
        self.assertEqual(source, "rule")
        self.assertIsNone(refs[0]["title"])
        self.assertEqual(refs[1]["year"], 2011)  # 规则年份尽力而为

    def test_no_llm_flag(self):
        refs, source = rp.build_references(self.entries, use_llm=False)
        self.assertEqual(source, "rule")
        self.assertEqual(refs[0]["doi"], "10.1038/nature04236")


class TestEndToEnd(unittest.TestCase):
    def test_prepare_and_dump(self):
        blocks = [
            ProcessedBlock("paragraph", content="Body [1]."),
            ProcessedBlock("heading", content="References"),
            ProcessedBlock("reference",
                           content="[1] A. Author, T., Nature 4 (2005) 366, "
                                   "https://doi.org/10.1038/nature04236."),
        ]
        payload = rp.prepare_references(blocks, use_llm=False)
        self.assertIsNotNone(payload)
        self.assertEqual(payload["source"], "rule")
        self.assertEqual(payload["count"], 1)
        with tempfile.TemporaryDirectory() as td:
            path = rp.dump_references(payload, Path(td))
            data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["references"][0]["n"], 1)
        self.assertEqual(data["references"][0]["doi"], "10.1038/nature04236")

    def test_no_references_no_file(self):
        blocks = [ProcessedBlock("paragraph", content="Just body text.")]
        self.assertIsNone(rp.prepare_references(blocks, use_llm=False))


class TestArxivAndTitleHygiene(unittest.TestCase):
    """arxiv_id 确定性抽取 + title 卫生（forecast 实测质量崩溃修复）。"""

    def test_arxiv_new_format(self):
        self.assertEqual(
            rp.extract_arxiv("[1] X. Y., Living Rev. Rel. 24, 4 (2021), "
                             "arXiv:2011.12414 [gr-qc]."),
            "2011.12414")
        self.assertEqual(
            rp.extract_arxiv("... arXiv:2605.22944v1 [astro-ph.CO]."),
            "2605.22944")

    def test_arxiv_old_format(self):
        # forecast entry[3] 真实形态
        self.assertEqual(
            rp.extract_arxiv('[3] S. Sarangi and S. H. H. Tye, Phys. Lett. B '
                             '536, 185 (2002) [arXiv:hep-th/0204074].'),
            "hep-th/0204074")
        self.assertIsNone(rp.extract_arxiv("[1] T. W. B. Kibble, J. Phys. A 9, "
                                           "1387 (1976)."))

    def test_title_strips_enum_prefix(self):
        self.assertEqual(rp._clean_title("[12] Some Real Title", "raw"),
                         "Some Real Title")
        self.assertEqual(rp._clean_title("12. Some Real Title", "raw"),
                         "Some Real Title")

    def test_title_arxiv_bracket_truncated(self):
        self.assertEqual(
            rp._clean_title("Cosmic Strings and More [arXiv:hep-th/0204074]",
                            "raw"),
            "Cosmic Strings and More")
        # 截断后只剩碎片 → None
        self.assertIsNone(rp._clean_title("Ab [arXiv:2011.12414]", "raw"))

    def test_title_raw_repeat_becomes_none(self):
        raw = "[1] T. W. B. Kibble, J. Phys. A 9, 1387 (1976)."
        self.assertIsNone(rp._clean_title(raw, raw))
        self.assertIsNone(rp._clean_title("", raw))
        self.assertIsNone(rp._clean_title(None, raw))

    def test_forecast_real_raws(self):
        # forecast 真实 raw 两条：LLM 返回正常 title 时卫生不破坏、
        # arxiv_id 正则兜住；LLM 失败降级时 title=None 但 arxiv_id 仍在
        entries = [
            {"n": 1, "raw": "[1] T. W. B. Kibble, J. Phys. A 9, 1387 (1976)."},
            {"n": 3, "raw": "[3] S. Sarangi and S. H. H. Tye, Phys. Lett. B 536, "
                            "185 (2002) [arXiv:hep-th/0204074]."},
        ]
        refs, source = rp.build_references(entries, use_llm=False)
        self.assertEqual(source, "rule")
        self.assertIsNone(refs[0]["title"])
        self.assertIsNone(refs[0]["arxiv_id"])
        self.assertEqual(refs[1]["arxiv_id"], "hep-th/0204074")


    def test_llm_empty_response_retried(self):
        # 空/非 JSON 响应重试一次后成功（martins2000 "Expecting value" 实测）
        import sys
        import types
        calls = {"n": 0}

        class _Msg:
            def __init__(self, c): self.content = c

        class _Choice:
            def __init__(self, c): self.message = _Msg(c)

        class _Resp:
            def __init__(self, c): self.choices = [_Choice(c)]

        class _Completions:
            def create(self, **_kw):
                calls["n"] += 1
                if calls["n"] == 1:
                    return _Resp("")   # 空响应
                return _Resp('[{"n": 1, "title": "T", "authors": [], '
                           '"year": 2005, "venue": "Nature", "doi": null}]')

        class _Chat:
            completions = _Completions()

        class _Client:
            def __init__(self, **kw): self.chat = _Chat()

        fake = types.ModuleType("openai")
        fake.OpenAI = _Client
        orig_mod = sys.modules.get("openai")
        orig_key = rp.config.DEEPSEEK_API_KEY
        rp.config.DEEPSEEK_API_KEY = "test-key"
        sys.modules["openai"] = fake
        try:
            items = rp._llm_extract([{"n": 1, "raw": "[1] A, T."}], use_llm=True)
        finally:
            rp.config.DEEPSEEK_API_KEY = orig_key
            if orig_mod is not None:
                sys.modules["openai"] = orig_mod
            else:
                del sys.modules["openai"]
        self.assertEqual(calls["n"], 2)
        self.assertIsNotNone(items)
        self.assertEqual(items[0]["title"], "T")


if __name__ == "__main__":
    unittest.main()
