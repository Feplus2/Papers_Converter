"""article_boundary 脏 PDF 边界切分单测（stub IR 块，无外部依赖）。

运行：
    python -m unittest test_article_boundary -v

原则验证：锚点强信号才动刀、固定段标题保护、页锚永不丢失。
"""

import unittest

from article_boundary import (
    LEAD_TRIM_MIN_PRE_CHARS,
    apply_article_boundary,
    trim_post_references_tail,
    trim_pre_title_content,
)


class B:
    """ProcessedBlock 最小 stub（只需 kind/content）"""

    def __init__(self, kind: str, content: str = ""):
        self.kind = kind
        self.content = content

    def __repr__(self):
        return f"<{self.kind}:{self.content[:20]}>"


_TITLE = "Rational design of layered oxide materials for sodium-ion batteries"
_PREV_JUNK = "This is the tail of the previous article with a long paragraph " * 8  # >400 字符


def _dirty_lead_blocks() -> list:
    """杂志截页：上一篇文章残留 → 本文标题 → 正文"""
    return [
        B("page_anchor", "1"),
        B("paragraph", _PREV_JUNK),
        B("heading", "Conclusions of the Previous Article"),
        B("paragraph", "More tail text of the previous article. " * 10),
        B("page_anchor", "2"),
        B("heading", _TITLE),
        B("paragraph", "Sodium-ion batteries have captured widespread attention."),
        B("heading", "References"),
        B("reference", "[1] Zhao et al."),
    ]


class TestLeadTrim(unittest.TestCase):

    def test_dirty_lead_trimmed_at_title_anchor(self):
        blocks = _dirty_lead_blocks()
        out = trim_pre_title_content(blocks, _TITLE)
        kinds = [b.kind for b in out]
        # 标题及其后内容保留
        self.assertIn(_TITLE, [b.content for b in out])
        self.assertNotIn("Conclusions of the Previous Article",
                         [b.content for b in out])
        # 页锚全部保留（页计数不失真）
        self.assertEqual(kinds.count("page_anchor"), 2)

    def test_normal_paper_untouched(self):
        """标题就是第一个内容块 → 不动刀"""
        blocks = [B("page_anchor", "1"), B("heading", _TITLE),
                  B("paragraph", "Abstract text here.")]
        out = trim_pre_title_content(blocks, _TITLE)
        self.assertEqual(len(out), len(blocks))

    def test_small_pre_content_untouched(self):
        """锚点前只有零星噪声（<400 字符）→ 不动刀"""
        blocks = [B("page_anchor", "1"), B("paragraph", "A short label."),
                  B("heading", _TITLE), B("paragraph", "Body.")]
        out = trim_pre_title_content(blocks, _TITLE)
        self.assertEqual(len(out), len(blocks))

    def test_short_or_empty_title_untouched(self):
        blocks = _dirty_lead_blocks()
        self.assertEqual(len(trim_pre_title_content(blocks, "")), len(blocks))
        self.assertEqual(len(trim_pre_title_content(blocks, "Short Title")),
                         len(blocks))

    def test_title_not_found_untouched(self):
        blocks = _dirty_lead_blocks()
        out = trim_pre_title_content(blocks, "A Completely Different Paper Title Here")
        self.assertEqual(len(out), len(blocks))

    def test_ocr_variant_title_matches(self):
        """去标点等价：OCR 把连字符识别变形仍能命中"""
        variant = _TITLE.replace("-", " ")
        blocks = _dirty_lead_blocks()
        blocks[5].content = variant
        out = trim_pre_title_content(blocks, _TITLE)
        self.assertNotIn("Conclusions of the Previous Article",
                         [b.content for b in out])


class TestTailTrim(unittest.TestCase):

    def test_next_article_after_references_trimmed(self):
        blocks = [
            B("heading", _TITLE), B("paragraph", "Body. " * 80),
            B("heading", "References"),
            B("reference", "[1] Zhao et al."), B("reference", "[2] Wang et al."),
            B("page_anchor", "9"),
            B("heading", "A Brand New Article About Something Else Entirely"),
            B("paragraph", "Abstract of the next article. " * 10),
        ]
        out = trim_post_references_tail(blocks)
        self.assertNotIn("A Brand New Article About Something Else Entirely",
                         [b.content for b in out])
        # 页锚保留
        self.assertIn("page_anchor", [b.kind for b in out])

    def test_acknowledgments_after_references_protected(self):
        """Science/PNAS：Acknowledgments 在 References 后合法 → 放弃尾切"""
        blocks = [
            B("heading", "References"), B("reference", "[1] A."),
            B("heading", "Acknowledgments"), B("paragraph", "We thank ..."),
            B("heading", "Some Other Section"),
        ]
        out = trim_post_references_tail(blocks)
        self.assertEqual(len(out), len(blocks))

    def test_no_references_heading_untouched(self):
        blocks = [B("heading", _TITLE), B("paragraph", "Body."),
                  B("heading", "Introduction")]
        out = trim_post_references_tail(blocks)
        self.assertEqual(len(out), len(blocks))

    def test_nothing_after_references_untouched(self):
        blocks = [B("heading", "References"), B("reference", "[1] A.")]
        out = trim_post_references_tail(blocks)
        self.assertEqual(len(out), len(blocks))

    def test_paragraph_between_refs_and_next_title_not_enough(self):
        """尾切只认 References 后的非固定段标题；无标题则不动刀"""
        blocks = [B("heading", "References"), B("reference", "[1] A."),
                  B("paragraph", "Some trailing text without heading.")]
        out = trim_post_references_tail(blocks)
        self.assertEqual(len(out), len(blocks))


class TestApplyBoundary(unittest.TestCase):

    def test_full_dirty_pdf_both_ends(self):
        blocks = _dirty_lead_blocks() + [
            B("page_anchor", "8"),
            B("heading", "Next Article Title That Is Not Ours At All"),
            B("paragraph", "Next article body. " * 20),
        ]
        out = apply_article_boundary(blocks, _TITLE)
        contents = [b.content for b in out]
        self.assertIn(_TITLE, contents)
        self.assertNotIn("Conclusions of the Previous Article", contents)
        self.assertNotIn("Next Article Title That Is Not Ours At All", contents)
        # References 与引文保留
        self.assertIn("References", contents)
        self.assertIn("[1] Zhao et al.", contents)

    def test_clean_paper_fully_preserved(self):
        blocks = [
            B("page_anchor", "1"), B("heading", _TITLE),
            B("paragraph", "Abstract. " * 50),
            B("heading", "Introduction"), B("paragraph", "Body. " * 100),
            B("heading", "References"), B("reference", "[1] A."),
            B("heading", "Acknowledgments"), B("paragraph", "Thanks."),
        ]
        out = apply_article_boundary(blocks, _TITLE)
        self.assertEqual(len(out), len(blocks))


if __name__ == "__main__":
    unittest.main()
