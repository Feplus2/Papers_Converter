"""cover_detect 封面判定回归测试。

运行：
    python -m unittest test_cover_detect -v

合成样例自包含（任何环境可跑）；真实事故样本来自本机 zotero-brain 解析
缓存，不在本机时自动 skip。用例即 2026-08-12 zhao2020 整页丢失事故的
回归防线，见 docs/structure-detection.md。
"""

import json
import os
import unittest
from pathlib import Path

from cover_detect import detect_cover_pages, legacy_detect_cover_pages
from structure_llm import arbitrate, page_digest

# 真实样本目录经 ZOTERO_PARSED_DIR 指定（开发机路径不入库；不设或不存在时自动 skip）
_PARSED = Path(os.environ.get("ZOTERO_PARSED_DIR", "zotero_parsed_not_found"))


def _text_block(text: str) -> dict:
    return {"type": "text", "text": text, "page_idx": 0}


def _cover_like_page(markers: list[str], extra_lines: list[str] | None = None,
                     page_idx: int = 0) -> list[dict]:
    """构造 TU/e Pure 风格的仓库封面页（短模板行，无正文信号）"""
    lines = ["Delft University of Technology", "Some Paper Title About Things",
             "Author One; Author Two", "10.1234/example.2020"]
    lines += markers
    if extra_lines:
        lines += extra_lines
    return [{"type": "text", "text": t, "page_idx": page_idx} for t in lines]


_ALL_MARKERS = [
    "Citation (APA): One, A., & Two, B. (2020). Some paper title about things.",
    "Document Version: Accepted author manuscript",
    "Important note: To cite this publication, please use the final published version.",
    "Takedown policy: Please contact us if this document breaches copyrights.",
    "Downloaded from repository on 2026-08-12",
    "For technical reasons the full text has been split.",
    "University of Technology repository",
]


class TestCoverDetectSynthetic(unittest.TestCase):

    def test_real_cover_full_template_detected(self):
        """真封面：模板全套（7/7 标记）、无正文信号 → 切除，
        即使权威标题/DOI 出现在封面上（模板会列出它们）"""
        blocks = _cover_like_page(_ALL_MARKERS)
        self.assertEqual(detect_cover_pages(
            blocks, title="Some Paper Title About Things",
            doi="10.1234/example.2020"), {0})

    def test_only_page0_eligible(self):
        """封面只可能在首页：page 1 即使标记齐全也永不判封面
        （zhao2020 事故的直接根因防线）"""
        blocks = _cover_like_page(_ALL_MARKERS, page_idx=1)
        self.assertEqual(detect_cover_pages(blocks), set())

    def test_two_markers_not_enough(self):
        """旧阈值 2 个标记即丢页是事故根因；新阈值 3 以下不判"""
        blocks = _cover_like_page([_ALL_MARKERS[4], _ALL_MARKERS[6]])
        self.assertEqual(detect_cover_pages(blocks), set())

    def test_long_prose_veto(self):
        """标记够数但散文超阈值（摘要/正文形态）→ 保留"""
        blocks = _cover_like_page(_ALL_MARKERS[:4])
        blocks.append(_text_block("Sodium-ion batteries have captured widespread "
                                  "attention for grid-scale energy storage owing to "
                                  "the natural abundance of sodium. " * 20))
        self.assertEqual(detect_cover_pages(blocks), set())

    def test_long_block_veto(self):
        """单个长散文块（>300 字符）→ 保留"""
        blocks = _cover_like_page(_ALL_MARKERS[:4])
        blocks.append(_text_block("A" * 301))
        self.assertEqual(detect_cover_pages(blocks), set())

    def test_rich_block_veto(self):
        """页内含 image/equation/table 块 → 必为正文页"""
        for rich in ("image", "equation", "table"):
            blocks = _cover_like_page(_ALL_MARKERS[:4])
            blocks.append({"type": rich, "text": "", "page_idx": 0})
            self.assertEqual(detect_cover_pages(blocks), set(), rich)

    def test_section_heading_veto(self):
        """编号章节标题（"1. Introduction"）→ 保留"""
        blocks = _cover_like_page(_ALL_MARKERS[:4])
        blocks.append(_text_block("1. Introduction"))
        self.assertEqual(detect_cover_pages(blocks), set())

    def test_anchor_veto_weak_markers_with_title(self):
        """标记偏弱（<5）且权威标题在页内 → 保留（更像真首页）"""
        blocks = _cover_like_page(_ALL_MARKERS[:3])
        self.assertEqual(detect_cover_pages(
            blocks, title="Some Paper Title About Things"), set())

    def test_anchor_not_applied_for_strong_markers(self):
        """标记 >=5（模板全套）时锚定不生效——真封面也列标题"""
        blocks = _cover_like_page(_ALL_MARKERS[:6])
        self.assertEqual(detect_cover_pages(
            blocks, title="Some Paper Title About Things"), {0})

    def test_empty_content_list(self):
        self.assertEqual(detect_cover_pages([]), set())

    def test_normal_title_page_with_download_header(self):
        """Science 式首页：页眉 'Downloaded from ...' + 正文摘要段
        （zhao2020 page 0 形态，旧规则侥幸逃过，新规则明确放行）"""
        blocks = [
            _text_block("Rational design of layered oxide materials for sodium-ion batteries"),
            _text_block("Chenglong Zhao, Qidi Wang, Zhenpeng Yao"),
            _text_block("Sodium-ion batteries have captured widespread attention "
                        "for grid-scale energy storage owing to the natural abundance "
                        "of sodium resources. " * 15),
            {"type": "header", "text": "Downloaded from science.org on August 12, 2026",
             "page_idx": 0},
            {"type": "image", "text": "", "page_idx": 0},
        ]
        self.assertEqual(detect_cover_pages(blocks), set())


@unittest.skipUnless((_PARSED / "26NNZJHX").exists(), "zotero-brain 解析缓存不在本机")
class TestCoverDetectRealSamples(unittest.TestCase):
    """真实语料回归：真封面切除 + 事故页保全"""

    def _load(self, key: str) -> list[dict]:
        cl = list((_PARSED / key).glob("*_content_list.json"))
        return json.load(open(cl[0], encoding="utf-8"))

    def test_real_cover_26nnzjhx_removed(self):
        """TU/e 仓库真封面（7/7 标记）→ 切除"""
        data = self._load("26NNZJHX")
        self.assertEqual(detect_cover_pages(
            data, title="Rational design of layered oxide materials "
                        "for sodium-ion batteries"), {0})

    def test_incident_d9avt22j_preserved(self):
        """旧规则误杀 page 1 的事故篇 → 新规则零切除"""
        data = self._load("D9AVT22J")
        self.assertEqual(legacy_detect_cover_pages(data), {1})  # 旧规则确实误杀
        self.assertEqual(detect_cover_pages(data), set())

    def test_incident_l2krj8kz_preserved(self):
        data = self._load("L2KRJ8KZ")
        self.assertEqual(legacy_detect_cover_pages(data), {1})
        self.assertEqual(detect_cover_pages(data), set())


class TestStructureLlmArbitration(unittest.TestCase):
    """辅助模型仲裁纯逻辑（无网络；保守方向优先）"""

    def test_llm_non_cover_overturns_rule(self):
        """LLM 判非封面 → 撤销规则判定（宁可保留疑似噪声页）"""
        self.assertEqual(arbitrate({0}, {"is_cover": False, "confidence": "low"}), set())

    def test_llm_cover_confirms_rule(self):
        self.assertEqual(arbitrate({0}, {"is_cover": True, "confidence": "low"}), {0})

    def test_llm_cover_adds_only_high_confidence(self):
        """规则未判时，LLM 判封面需 high 置信才采纳"""
        self.assertEqual(arbitrate(set(), {"is_cover": True, "confidence": "high"}), {0})
        self.assertEqual(arbitrate(set(), {"is_cover": True, "confidence": "medium"}), set())

    def test_llm_answer_malformed_falls_back_to_no_cover(self):
        """缺字段按 is_cover=False 处理（保守方向）"""
        self.assertEqual(arbitrate({0}, {}), set())

    def test_page_digest_marks_rich_blocks(self):
        """块类型标注保留（LLM 判页面角色的关键信号）"""
        blocks = [
            {"type": "text", "text": "Some title", "page_idx": 0},
            {"type": "image", "text": "", "page_idx": 0},
            {"type": "header", "text": "Downloaded from x", "page_idx": 0},
            {"type": "equation", "text": "$x^2$", "page_idx": 1},
        ]
        digest = page_digest(blocks)
        self.assertIn("[image]", digest)
        self.assertIn("[equation]", digest)
        self.assertNotIn("Downloaded from", digest)  # 噪声块不入摘要
        self.assertIn("=== page 0 ===", digest)


if __name__ == "__main__":
    unittest.main()
