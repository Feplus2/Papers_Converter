"""MAX_PATH 防护回归（Zotero 批量导入用户实测病例）。

Zotero 按条目标题重命名的 PDF 可超 120 字符；staging 目录与引擎内层产物
（{stem}_content_list.json、images/<64位哈希>.jpg）全派生自 stem，
双层叠加超 Windows MAX_PATH=260 → [Errno 2]（标题 ≤84 全成功、≥112 全失败，
分界线精确落在 260）。

修复（pipeline._staging_paths）：短 stem 沿用 {stem}-{digest}；超长时
staging 用 {stem[:24]}-{digest} 短名 + 引擎硬链接别名（内层产物随之短路径）。

运行: python test_maxpath_staging.py
"""

import tempfile
import unittest
from pathlib import Path

from pipeline import _staging_paths

# 用户环境的真实 base 深度（SageRead appdata 下的 papers-converter）
DEEP_BASE = Path("C:/Users/20995/AppData/Roaming/com.bettersageread.dev/papers-converter")


class TestStagingPaths(unittest.TestCase):
    def _mkpdf(self, stem: str) -> Path:
        d = Path(tempfile.mkdtemp())
        p = d / f"{stem}.pdf"
        p.write_bytes(b"%PDF-1.4 fake content for hashing")
        return p

    def test_short_stem_unchanged(self):
        pdf = self._mkpdf("grollius2022optimized")
        staging, engine = _staging_paths(pdf, DEEP_BASE)
        self.assertTrue(staging.name.startswith("grollius2022optimized-"))
        self.assertEqual(engine, pdf)  # 短 stem 不建别名，原样直通
        # 不触发目录创建（短路返回，无副作用）
        self.assertFalse(staging.exists())

    def test_long_stem_shortened_and_aliased(self):
        pdf = self._mkpdf("A" * 120 + " Very Long Paper Title " + "x" * 80)
        staging, engine = _staging_paths(pdf, DEEP_BASE)
        self.assertLessEqual(len(staging.name), 31)  # 24 + 1 + 6
        self.assertTrue(str(engine).endswith(".pdf"))
        self.assertEqual(engine.parent, staging)
        self.assertTrue(engine.exists())
        self.assertEqual(engine.read_bytes(), pdf.read_bytes())  # 硬链接/副本内容一致
        # 内层产物名派生自别名 stem：最深路径也得 <260
        inner_cl = staging / f"{engine.stem}_content_list.json"
        inner_img = staging / "images" / ("a" * 64 + ".jpg")
        self.assertLess(len(str(inner_cl)), 260)
        self.assertLess(len(str(inner_img)), 260)

    def test_boundary_40_chars_not_aliased(self):
        pdf = self._mkpdf("b" * 40)
        staging, engine = _staging_paths(pdf, DEEP_BASE)
        self.assertEqual(engine, pdf)
        self.assertTrue(staging.name.startswith("b" * 40 + "-"))

    def test_xml_suffix_preserved(self):
        d = Path(tempfile.mkdtemp())
        p = d / ("c" * 120 + ".xml")
        p.write_bytes(b"<article/>")
        _staging, engine = _staging_paths(p, DEEP_BASE)
        self.assertTrue(str(engine).endswith(".xml"))
        self.assertTrue(engine.exists())


if __name__ == "__main__":
    unittest.main()
