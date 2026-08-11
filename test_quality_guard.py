"""quality_guard 退化检测 + qc_paper 完整性闸单测。

运行：
    python -m unittest test_quality_guard -v
    # 或 pytest test_quality_guard.py

真实样本来自 SageRead 侧存证（VLM 失控事故论文 + 两篇正常论文），
不在本机时自动 skip；合成样例自包含，任何环境可跑。
"""

import json
import os
import tempfile
import unittest
from pathlib import Path

from quality_guard import (
    MAX_STAGE1_RETRIES,
    check_staging_dir,
    find_degenerate_loop,
)
from qc_paper import (
    PAGE_COMPLETENESS_RATIO,
    _check_page_completeness,
    qc_severe_findings,
)

_SAGEREAD_BOOKS = Path(os.environ.get(
    "SAGEREAD_BOOKS_DIR",
    r"C:/Users/20995/AppData/Roaming/com.xincmm.sageread.dev/books"))
# 真实事故样本：LED 波长列从真实 1700 nm 被编造递增到 15800+ nm
INCIDENT_MD = _SAGEREAD_BOOKS / "e69eb8d5ef7bb450" / "paper.md"
# 正常论文（不得命中）
NORMAL_MDS = [
    _SAGEREAD_BOOKS / "600c68f849caa135" / "paper.md",
    _SAGEREAD_BOOKS / "a27b187c6bd02d3c" / "paper.md",
]


def _wide_table(cols: int = 12, rows: int = 30) -> str:
    """逼真宽表：单元格长度不一（作者/标题/年份/DOI），无签名周期。"""
    words = ["Smith", "de las Casas", "Li", "Watanabe", "Garcia-Lopez", "Chen"]
    titles = ["Optical absorption in complex oxides",
              "A short note",
              "High-throughput discovery of novel perovskite phases",
              "On the thermodynamic stability of quaternary compounds"]
    lines = []
    header = "| " + " | ".join(f"Column {c} header" for c in range(cols)) + " |"
    lines.append(header)
    lines.append("|" + "|".join("---" for _ in range(cols)) + "|")
    for r in range(rows):
        cells = []
        for c in range(cols):
            if c % 4 == 0:
                cells.append(words[(r + c) % len(words)] + f" et al. ({1970 + (r * 7 + c) % 50})")
            elif c % 4 == 1:
                cells.append(titles[(r + c) % len(titles)])
            elif c % 4 == 2:
                cells.append(f"10.{1000 + r * 13 + c}/journal.{r}{c}x{100 + r}")
            else:
                cells.append(f"{(r * c + 3) % 97}.{(r + c) % 9}")
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


class TestFindDegenerateLoop(unittest.TestCase):
    def test_fire_synthetic_hits(self):
        body = "Some intro paragraph.\n\n" + "fire " * 200 + "\n\nTrailing."
        f = find_degenerate_loop(body)
        self.assertIsNotNone(f)
        self.assertGreaterEqual(f.repeats, 10)
        self.assertGreaterEqual(f.repeats * f.period, 300)
        self.assertIn("fire", f.preview)

    def test_nm_sequence_synthetic_hits(self):
        # 事故形态：数字递增（精确重复检测抓不到，签名周期法可抓）
        seq = ", ".join(f"{1700 + i * 100} nm" for i in range(150))
        f = find_degenerate_loop("LED wavelengths: " + seq)
        self.assertIsNotNone(f)

    def test_wide_table_no_hit(self):
        body = _wide_table()
        # 前置条件：表里确有 ≥200 字符的长行（否则测试无意义）
        self.assertTrue(any(len(l) >= 200 for l in body.split("\n")))
        self.assertIsNone(find_degenerate_loop(body))

    def test_table_separator_line_no_hit(self):
        # "|---|---|..." 周期短（4）但跨度有限：60 列仅 241 字符 < 300 下限
        sep = "|" + "|".join("---" for _ in range(60)) + "|"
        self.assertGreaterEqual(len(sep), 200)
        # 数据行单元格长度参差（逼真表格），唯一的均匀行就是分隔行
        cells = ["Smith et al. (2021)", "7", "A short note",
                 "High-throughput discovery of novel perovskite phases",
                 "10.1021/jacs.3c012", "0.97"]
        rows = ["| " + " | ".join(cells[(r + c) % len(cells)]
                                   for c in range(60)) + " |"
                for r in range(3)]
        self.assertIsNone(find_degenerate_loop("\n".join(rows + [sep])))

    def test_normal_prose_no_hit(self):
        para = ("The quest to identify materials with tailored properties is "
                "increasingly expanding into high-order composition spaces, with a "
                "corresponding combinatorial explosion in the number of candidate "
                "materials that must be considered by any search strategy. ")
        body = "\n\n".join(para * 3)  # 段落级重复（分属不同行）不算退化
        self.assertIsNone(find_degenerate_loop(body))

    def test_short_lines_ignored(self):
        # 同样的失控内容折成短行（<200）则不在检测范围（按设计）
        body = "\n".join("fire " * 20 for _ in range(50))
        self.assertIsNone(find_degenerate_loop(body))

    @unittest.skipUnless(INCIDENT_MD.exists(), "SageRead 事故样本不在本机")
    def test_incident_sample_hits(self):
        f = find_degenerate_loop(INCIDENT_MD.read_text(encoding="utf-8"))
        self.assertIsNotNone(f, "真实事故样本（nm 波长列编造递增）应命中")
        self.assertGreaterEqual(f.repeats, 10)
        self.assertGreaterEqual(f.repeats * f.period, 300)

    def test_normal_samples_no_hit(self):
        for md in NORMAL_MDS:
            if not md.exists():
                self.skipTest("SageRead 正常样本不在本机")
            with self.subTest(file=md):
                self.assertIsNone(
                    find_degenerate_loop(md.read_text(encoding="utf-8")))


class TestCheckStagingDir(unittest.TestCase):
    def test_md_hit(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "paper.md").write_text(
                "intro\n\n" + "fire " * 200, encoding="utf-8")
            self.assertIsNotNone(check_staging_dir(d))

    def test_content_list_fallback_hit(self):
        # 无 md 时退而扫描 content_list 文本字段
        with tempfile.TemporaryDirectory() as d:
            blocks = [{"type": "text", "text": ", ".join(
                f"{1700 + i * 100} nm" for i in range(150))}]
            (Path(d) / "paper_content_list.json").write_text(
                json.dumps(blocks), encoding="utf-8")
            self.assertIsNotNone(check_staging_dir(d))

    def test_clean_no_hit(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "paper.md").write_text(
                "# Title\n\nNormal paragraph, nothing suspicious.", encoding="utf-8")
            self.assertIsNone(check_staging_dir(d))

    def test_max_retries_is_two(self):
        self.assertEqual(MAX_STAGE1_RETRIES, 2)


class TestPageCompleteness(unittest.TestCase):
    """页数对照（qc_paper._check_page_completeness）：整页内容丢失判严重。"""

    def _body(self, n_markers: int) -> str:
        return "\n".join(f"<!-- page: {i + 1} -->" for i in range(n_markers))

    def test_3_of_5_hits(self):
        # zhao2020 真实事故形态：5 页论文重解析后仅剩 3 页标记
        # 3 <= int(5*0.6)=3 → 命中（阈值取 <= 正是为这个案例）
        self.assertEqual(int(5 * PAGE_COMPLETENESS_RATIO), 3)
        self.assertTrue(_check_page_completeness(self._body(3), 5))

    def test_5_of_6_no_hit(self):
        # 5 <= int(6*0.6)=3 不成立 → 不命中（0.6 只防大开裂）
        self.assertFalse(_check_page_completeness(self._body(5), 6))

    def test_full_pages_no_hit(self):
        self.assertFalse(_check_page_completeness(self._body(5), 5))

    def test_unknown_pdf_pages_skipped(self):
        # 没有 PDF 页数时无法对照，不查（返回空）
        self.assertFalse(_check_page_completeness(self._body(0), None))
        self.assertFalse(_check_page_completeness(self._body(0), 0))

    def test_tiny_pdf_skipped(self):
        # ≤2 页论文不查：完整产物的标记数 ≈ 页数-1（首页前导锚点被剥），
        # 1~2 页完整也只有 0~1 个标记，查则必然误报
        self.assertFalse(_check_page_completeness(self._body(0), 1))
        self.assertFalse(_check_page_completeness(self._body(1), 2))


class TestSevereFindings(unittest.TestCase):
    """qc_severe_findings：图/表断号 + 页数不足 → 严重级。"""

    def _write_md(self, d: Path, body: str) -> Path:
        md = d / "paper.md"
        md.write_text(body, encoding="utf-8")
        return md

    def test_fig_gap_is_severe(self):
        # 有 Figure 1 / Figure 4 缺 Figure 2/3 → 断号严重级
        body = ("Opening paragraph about layered oxides.\n\n"
                "Figure 1: Crystal structure of the layered oxide.\n\n"
                "Some discussion of the results.\n\n"
                "Figure 4: Galvanostatic charge-discharge profiles.\n\n"
                "<!-- page: 1 -->")
        with tempfile.TemporaryDirectory() as td:
            severe = qc_severe_findings(self._write_md(Path(td), body), 1)
        gaps = [s for s in severe if "断号" in s]
        self.assertEqual(len(gaps), 2)  # 缺 Figure 2、Figure 3 各一条
        self.assertTrue(any("缺 Figure 2" in s for s in gaps))
        self.assertTrue(any("缺 Figure 3" in s for s in gaps))

    def test_page_loss_is_severe(self):
        body = "\n".join(["Normal paragraph."] +
                         [f"<!-- page: {i + 1} -->" for i in range(3)])
        with tempfile.TemporaryDirectory() as td:
            severe = qc_severe_findings(self._write_md(Path(td), body), 5)
        self.assertTrue(any("页标记 3/5" in s for s in severe))

    def test_clean_no_severe(self):
        body = ("# Title\n\nA normal paragraph, nothing suspicious.\n\n"
                "Figure 1: The only figure, properly numbered.\n\n"
                "<!-- page: 1 -->")
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(qc_severe_findings(self._write_md(Path(td), body), 1), [])

    def test_frontmatter_not_counted(self):
        # frontmatter 里的 "page:" 之类不纳入页标记统计
        body = ("---\ntitle: Demo\n---\nParagraph.\n\n<!-- page: 1 -->")
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(qc_severe_findings(self._write_md(Path(td), body), 1), [])

    def test_missing_file_no_severe(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(qc_severe_findings(Path(td) / "nope.md", 5), [])


class TestPipelineRetry(unittest.TestCase):
    """convert_pdf 全链路（桩 provider，离线）：重试协议 + done 打标。"""

    DEGEN_TEXT = "fire " * 200
    CLEAN_TEXT = ("A normal paragraph about oxide discovery, with varied words "
                  "and no machine-gun repetition at all. " * 3)

    def setUp(self):
        # 测试必须离线确定：清掉 .env 注入的 MinerU Token，
        # 否则"重试仍失败→自动降级 MinerU pipeline"会真打 API 并覆盖 staging，
        # 断言结果取决于网络/配额（曾在本机因此误失败）
        import config
        self._saved_mineru_token = config.MINERU_TOKEN
        config.MINERU_TOKEN = ""

    def tearDown(self):
        import config
        config.MINERU_TOKEN = self._saved_mineru_token

    def _make_pdf(self, d: Path) -> Path:
        import fitz
        pdf = d / "sample.pdf"
        doc = fitz.open()
        doc.new_page()
        doc.save(pdf)
        doc.close()
        return pdf

    def _run(self, degen_parses: int):
        """degen_parses: 前 N 次 parse 产出退化产物（之后转正常）。"""
        import io
        import contextlib
        import ocr_provider
        import pipeline

        outer = self

        class StubProvider:
            name = "stubdegen"
            calls = 0

            def parse(self, pdf_path, work_dir, ocr=True, progress=None, **opts):
                StubProvider.calls += 1
                wd = Path(work_dir)
                (wd / "images").mkdir(parents=True, exist_ok=True)
                stem = Path(pdf_path).stem
                degen = StubProvider.calls <= degen_parses
                text = outer.DEGEN_TEXT if degen else outer.CLEAN_TEXT
                (wd / f"{stem}.md").write_text(text, encoding="utf-8")
                (wd / f"{stem}_content_list.json").write_text(json.dumps(
                    [{"type": "text", "text": text, "page_idx": 0}],
                    ensure_ascii=False), encoding="utf-8")
                return {"content_list": [], "images_dir": "", "markdown": text}

        ocr_provider.register("stubdegen", StubProvider)
        with tempfile.TemporaryDirectory() as td:
            pdf = self._make_pdf(Path(td))
            out = Path(td) / "out"
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                result = pipeline.convert_pdf(
                    pdf, out, use_llm=False, provider_name="stubdegen",
                    headless=True)
            events = [json.loads(l) for l in buf.getvalue().splitlines() if l.strip()]
        return result, events, StubProvider.calls

    def test_retry_then_clean(self):
        # 前 2 次退化、第 3 次正常 → 2 条重试 progress，done 无 degenerate
        result, events, calls = self._run(degen_parses=2)
        self.assertIsNotNone(result)
        self.assertEqual(calls, 3)
        retries = [e for e in events if e["type"] == "progress"
                   and "正在重试 OCR" in e.get("detail", "")]
        self.assertEqual([e["detail"] for e in retries], [
            "检测到异常重复内容，正在重试 OCR（第 1 次）",
            "检测到异常重复内容，正在重试 OCR（第 2 次）",
        ])
        done = [e for e in events if e["type"] == "done"]
        self.assertEqual(len(done), 1)
        self.assertNotIn("degenerate", done[0])
        # percent 全程单调不减
        percents = [e["percent"] for e in events if "percent" in e]
        self.assertEqual(percents, sorted(percents))

    def test_persistent_degenerate_flagged(self):
        # 3 次全退化 → 打回上限后放行，done 打标 degenerate
        result, events, calls = self._run(degen_parses=99)
        self.assertIsNotNone(result)
        self.assertEqual(calls, 1 + MAX_STAGE1_RETRIES)
        done = [e for e in events if e["type"] == "done"]
        self.assertEqual(len(done), 1)
        self.assertIs(done[0].get("degenerate"), True)


class TestPipelineIntegrityGate(unittest.TestCase):
    """交付前完整性闸（桩 provider，离线）：断号/丢页 → 重试 → 打标 incomplete。"""

    # 正文引用 Fig. 1 / Fig. 4 但全文无任何实际图块/图注 → 断号（严重级）。
    # 注意桩产物不能写成 "Figure N: caption" 独立行——content_processor 会把
    # 这类行首图注行归并到图片块，无图片时整行丢弃，断号反而探测不到
    INCOMPLETE_TEXT = (
        "Opening paragraph about layered oxide cathodes, with varied words. "
        "As shown in Fig. 1, the structure is layered. "
        "The electrochemical performance is summarized in Fig. 4, "
        "while intermediate data illustrate the trend.")
    COMPLETE_TEXT = ("A normal paragraph about oxide discovery, with varied words "
                     "and no machine-gun repetition at all. " * 3)

    def setUp(self):
        # 同 TestPipelineRetry：隔离 MinerU Token，保证离线确定
        import config
        self._saved_mineru_token = config.MINERU_TOKEN
        config.MINERU_TOKEN = ""

    def tearDown(self):
        import config
        config.MINERU_TOKEN = self._saved_mineru_token

    def _make_pdf(self, d: Path) -> Path:
        import fitz
        pdf = d / "sample.pdf"
        doc = fitz.open()
        doc.new_page()
        doc.save(pdf)
        doc.close()
        return pdf

    def _run(self, incomplete_parses: int):
        """incomplete_parses: 前 N 次 parse 产出不完整产物（之后转完整）。"""
        import io
        import contextlib
        import ocr_provider
        import pipeline

        outer = self

        class StubProvider:
            name = "stubincomplete"
            calls = 0

            def parse(self, pdf_path, work_dir, ocr=True, progress=None, **opts):
                StubProvider.calls += 1
                wd = Path(work_dir)
                (wd / "images").mkdir(parents=True, exist_ok=True)
                stem = Path(pdf_path).stem
                incomplete = StubProvider.calls <= incomplete_parses
                text = outer.INCOMPLETE_TEXT if incomplete else outer.COMPLETE_TEXT
                (wd / f"{stem}.md").write_text(text, encoding="utf-8")
                (wd / f"{stem}_content_list.json").write_text(json.dumps(
                    [{"type": "text", "text": text, "page_idx": 0}],
                    ensure_ascii=False), encoding="utf-8")
                return {"content_list": [], "images_dir": "", "markdown": text}

        ocr_provider.register("stubincomplete", StubProvider)
        with tempfile.TemporaryDirectory() as td:
            pdf = self._make_pdf(Path(td))
            out = Path(td) / "out"
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                result = pipeline.convert_pdf(
                    pdf, out, use_llm=False, provider_name="stubincomplete",
                    headless=True)
            events = [json.loads(l) for l in buf.getvalue().splitlines() if l.strip()]
        return result, events, StubProvider.calls

    def test_clean_first_pass_no_retry(self):
        # 首次即完整 → 不重跑，done 无 incomplete
        result, events, calls = self._run(incomplete_parses=0)
        self.assertIsNotNone(result)
        self.assertEqual(calls, 1)
        done = [e for e in events if e["type"] == "done"]
        self.assertEqual(len(done), 1)
        self.assertNotIn("incomplete", done[0])

    def test_retry_then_complete(self):
        # 前 2 次不完整、第 3 次完整 → 2 次重跑，done 无 incomplete
        result, events, calls = self._run(incomplete_parses=2)
        self.assertIsNotNone(result)
        self.assertEqual(calls, 1 + MAX_STAGE1_RETRIES)
        retries = [e for e in events if e["type"] == "progress"
                   and "重新解析" in e.get("detail", "")]
        self.assertEqual(len(retries), MAX_STAGE1_RETRIES)
        done = [e for e in events if e["type"] == "done"]
        self.assertEqual(len(done), 1)
        self.assertNotIn("incomplete", done[0])

    def test_persistent_incomplete_flagged(self):
        # 重跑仍不完整 + 降级不可用（无 Token）→ 交付但 done 打标
        result, events, calls = self._run(incomplete_parses=99)
        self.assertIsNotNone(result)
        self.assertEqual(calls, 1 + MAX_STAGE1_RETRIES)
        done = [e for e in events if e["type"] == "done"]
        self.assertEqual(len(done), 1)
        self.assertIs(done[0].get("incomplete"), True)
        warnings = done[0].get("qc_warnings")
        self.assertIsInstance(warnings, list)
        self.assertTrue(any("断号" in w for w in warnings))


if __name__ == "__main__":
    unittest.main()
