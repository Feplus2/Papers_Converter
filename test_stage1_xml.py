# -*- coding: utf-8 -*-
"""XML 管线（stage1_xml + mathml_tex + convert_xml）测试——全部本地 fixture，不触网。

fixtures（data/xml_fixtures/）：
  jats_mathml_sample.xml  合成 JATS：tex-math 优先 / MathML 兜底 / 本地图 / 结构化 ref
  elsevier_sample.xml     合成 Elsevier ce: 变体：MathML 公式 / 表格 / 本地图+外链图 / ce:bib
  PMC11209695.xml         真实 PMC JATS（Europe PMC fullTextXML，图/公式为 graphic 形态）
  PMC10955900.xml         真实 PMC JATS（第二篇回归）

覆盖：
  1. MathML→LaTeX 常见构造 + U+2212 归一 + 未知元素降级
  2. JATS 解析：meta / 公式双通道 / 段内公式拆块 / 本地图拷贝 / 结构化参考文献
  3. Elsevier 变体：meta（pubdate 优先于正文年份）/ 公式 / 表格 / 外链图降级
  4. 真实 PMC：双 graphic 去重（图注不双发）/ 上标引文归一 / 26 条参考文献
  5. convert_xml 端到端（--no-llm）：paper.md 契约字段 + $$..$$ 公式 + 图 + references.json
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parent))

import stage1_xml  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "data" / "xml_fixtures"


def _parse_to_staging(name: str):
    tmp = tempfile.mkdtemp()
    staging = Path(tmp) / "staging"
    cl_path = stage1_xml.parse(FIXTURES / name, staging)
    blocks = json.loads(cl_path.read_text(encoding="utf-8"))
    meta = json.loads((staging / "xml_meta.json").read_text(encoding="utf-8"))
    refs = json.loads((staging / "xml_references.json").read_text(encoding="utf-8"))
    return blocks, meta, refs, staging


class TestMathmlToLatex(unittest.TestCase):
    def _tex(self, fragment: str) -> str:
        import re as _re
        return _re.sub(r"\s+", " ", stage1_xml and __import__("mathml_tex").mathml_str_to_latex(fragment)).strip()

    def test_common_constructs(self):
        from mathml_tex import mathml_str_to_latex
        # 分式 + 上下标
        t = mathml_str_to_latex(
            '<math xmlns="http://www.w3.org/1998/Math/MathML">'
            '<mfrac><mi>E</mi><mrow><mi>k</mi><mi>T</mi></mrow></mfrac></math>')
        self.assertEqual(t.replace(" ", ""), r"\frac{E}{kT}")
        t2 = mathml_str_to_latex(
            '<math xmlns="http://www.w3.org/1998/Math/MathML">'
            '<msubsup><mi>x</mi><mn>1</mn><mn>2</mn></msubsup></math>')
        self.assertIn("x", t2.replace(" ", ""))
        self.assertIn("_1", t2.replace(" ", ""))
        self.assertIn("^2", t2.replace(" ", ""))

    def test_sqrt_and_op_mapping(self):
        from mathml_tex import mathml_str_to_latex
        t = mathml_str_to_latex(
            '<math xmlns="http://www.w3.org/1998/Math/MathML">'
            '<msqrt><mi>v</mi></msqrt></math>')
        self.assertEqual(t, r"\sqrt{v}")
        # × → \times、U+2212 → ASCII -
        t2 = mathml_str_to_latex(
            '<math xmlns="http://www.w3.org/1998/Math/MathML">'
            '<mrow><mi>a</mi><mo>×</mo><mo>−</mo><mi>b</mi></mrow></math>')
        flat = t2.replace(" ", "")
        self.assertIn(r"\times", flat)
        self.assertNotIn("\u2212", flat)
        self.assertIn("-", flat)

    def test_matrix(self):
        from mathml_tex import mathml_str_to_latex
        t = mathml_str_to_latex(
            '<math xmlns="http://www.w3.org/1998/Math/MathML">'
            '<mtable><mtr><mtd><mi>a</mi></mtd><mtd><mi>b</mi></mtd></mtr>'
            '<mtr><mtd><mi>c</mi></mtd><mtd><mi>d</mi></mtd></mtr></mtable></math>')
        self.assertIn(r"\begin{matrix}", t)
        self.assertIn("a & b", t.replace("  ", " "))
        self.assertIn(r"\\", t)

    def test_unknown_element_degrades_to_text(self):
        from mathml_tex import mathml_str_to_latex
        t = mathml_str_to_latex(
            '<math xmlns="http://www.w3.org/1998/Math/MathML">'
            '<mweirdattr><mi>x</mi></mweirdattr></math>')
        self.assertIn("x", t)  # 未知元素不丢内容


class TestJatsSample(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.blocks, cls.meta, cls.refs, cls.staging = _parse_to_staging("jats_mathml_sample.xml")

    def test_meta_fields(self):
        self.assertEqual(self.meta["doi"], "10.9999/jsf.2026.010101")
        self.assertEqual(self.meta["title"],
                         "Interfacial kinetics of model electrodes with Na layered oxides")
        self.assertEqual(self.meta["date"], "2026-01-20")
        self.assertEqual(self.meta["container-title"], "Journal of Synthetic Fixtures")
        self.assertEqual(self.meta["author"][0]["name"], "Anna B. Müller")
        self.assertEqual(self.meta["author"][1]["name"], "Li Wang")
        self.assertEqual(self.meta["volume"], "12")
        self.assertEqual(self.meta["page"], "101-118")
        self.assertIn("sodium-ion battery", self.meta["keywords"])
        self.assertTrue(self.meta["abstract"].startswith("We measure"))

    def test_tex_math_preferred_over_mathml(self):
        eqs = [b for b in self.blocks if b["type"] == "equation"]
        self.assertEqual(len(eqs), 2)
        # eq1: alternatives 里 tex-math 优先（含 \left[ 而非 MathML 兜底 j=j_0）
        self.assertIn(r"\left[", eqs[0]["text"])
        self.assertIn(r"\tag{1}", eqs[0]["text"])
        # eq2: 纯 MathML → Arrhenius 指数形态
        self.assertIn("frac", eqs[1]["text"])
        self.assertIn(r"\tag{2}", eqs[1]["text"])

    def test_inline_sup_sub_and_bibr(self):
        texts = [b.get("text", "") for b in self.blocks if b["type"] == "text"]
        joined = " ".join(texts)
        self.assertIn("j<sub>0</sub>", joined)  # sub 下标（HTML 形态；$..$ 转换在 content_processor）
        self.assertIn("[1](#ref-1)", joined)  # bibr 引文补 [] + 转跳链接重建
        self.assertIn("[2](#ref-2)", joined)
        self.assertIn("[3,4]", joined)        # 逗号合并形态的多值引文（无逐个拆链，诚实保留）
        self.assertNotIn("^{", joined)        # 不再产裸 ^{} 字面量

    def test_local_graphic_copied(self):
        imgs = [b for b in self.blocks if b["type"] in ("image", "chart")]
        self.assertEqual(len(imgs), 1)
        self.assertTrue(imgs[0]["img_path"].startswith("images/"))
        copied = self.staging / imgs[0]["img_path"]
        self.assertTrue(copied.exists())
        self.assertGreater(copied.stat().st_size, 50)

    def test_table_html(self):
        tables = [b for b in self.blocks if b["type"] == "table"]
        self.assertEqual(len(tables), 1)
        self.assertIn("<table>", tables[0]["table_body"])
        self.assertIn("R<sub>b</sub>", tables[0]["table_body"])
        self.assertIn("Table 1", tables[0]["table_caption"][0])

    def test_structured_references(self):
        self.assertEqual(self.refs["source"], "xml")
        self.assertEqual(self.refs["count"], 4)
        by_n = {r["n"]: r for r in self.refs["references"]}
        # B1: element-citation 结构化（doi/year/作者/标题）
        self.assertEqual(by_n[1]["doi"], "10.1016/j.joule.2018.11.011")
        self.assertEqual(by_n[1]["year"], 2019)
        self.assertEqual(by_n[1]["venue"], "Joule")
        self.assertEqual(by_n[1]["authors"][0], "Y You")
        self.assertIn("Na-ion storage", by_n[1]["title"])
        # B4: 无 element-citation → DOI 正则兜底
        self.assertEqual(by_n[4]["doi"], "10.9999/db.2022.007")
        # 正文流内的 ref_text 块（References 章节可见 + raw 带 [n] 前缀）
        ref_blocks = [b for b in self.blocks if b["type"] == "ref_text"]
        self.assertEqual(len(ref_blocks), 4)
        self.assertTrue(ref_blocks[0]["text"].startswith("[1] "))


class TestElsevierSample(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.blocks, cls.meta, cls.refs, cls.staging = _parse_to_staging("elsevier_sample.xml")

    def test_flavor_and_meta(self):
        # detect_flavor 直测
        root = stage1_xml.load_xml(FIXTURES / "elsevier_sample.xml")
        self.assertEqual(stage1_xml.detect_flavor(root), "elsevier")
        self.assertEqual(self.meta["doi"], "10.1016/j.jpowsour.2024.234567")
        self.assertEqual(self.meta["date"], "2024-06-15")  # pubdate，不误抓正文 2013
        self.assertEqual(self.meta["author"][0]["name"], "Wei Zhang")
        self.assertEqual(self.meta["container-title"], "Journal of Power Sources")

    def test_display_and_inline_formulas(self):
        eqs = [b for b in self.blocks if b["type"] == "equation"]
        self.assertEqual(len(eqs), 3)
        self.assertIn("frac{C", eqs[0]["text"].replace(" ", ""))
        self.assertIn(r"\tag{1}", eqs[0]["text"])
        self.assertIn("matrix", eqs[2]["text"])
        # 行内公式（$\sqrt{v}$）落在正文文本块
        texts = " ".join(b.get("text", "") for b in self.blocks if b["type"] == "text")
        self.assertIn("$\\sqrt{v}$", texts)
        # ce:inf 下标（单字符用紧凑形态 _b，PDF 路径同款）
        self.assertIn("R<sub>b</sub>", texts)

    def test_table_and_figures(self):
        tables = [b for b in self.blocks if b["type"] == "table"]
        self.assertEqual(len(tables), 1)
        self.assertIn("NNM-622", tables[0]["table_body"])
        # 本地图（fig1_sem.jpg）拷贝成功
        imgs = [b for b in self.blocks if b["type"] in ("image", "chart")]
        self.assertEqual(len(imgs), 1)
        self.assertTrue((self.staging / imgs[0]["img_path"]).exists())
        # 外链图降级：图注保成段落（零文本丢失）
        texts = [b.get("text", "") for b in self.blocks if b["type"] == "text"]
        self.assertTrue(any("Cycling curves at 1C" in t for t in texts))

    def test_bib_references(self):
        self.assertEqual(self.refs["count"], 3)
        dois = {r.get("doi") for r in self.refs["references"]}
        # 第 3 条 raw 无 DOI → None（诚实留空）；1/2 条正则提取
        self.assertEqual(dois, {None, "10.1039/c3ee40847g", "10.1039/C6CS00776G"})


class TestRealPmc(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.blocks, cls.meta, cls.refs, cls.staging = _parse_to_staging("PMC11209695.xml")

    def test_meta(self):
        self.assertEqual(self.meta["doi"], "10.1021/acsomega.4c00921")
        self.assertEqual(self.meta["container-title"], "ACS Omega")
        self.assertEqual(self.meta["author"][0]["name"], "Haowei Yao")
        self.assertIn("Simulation Study on Temperature Control", self.meta["title"])

    def test_double_graphic_dedup(self):
        """Europe PMC 每图 jpg+gif 双 graphic：图注只发一次（回归锁定）。"""
        cap_texts = [b.get("text", "") for b in self.blocks if b["type"] == "text"]
        fire_caps = [t for t in cap_texts if "Fire incidents at energy storage stations" in t]
        self.assertEqual(len(fire_caps), 1)

    def test_sup_citation_normalized(self):
        joined = " ".join(b.get("text", "") for b in self.blocks if b["type"] == "text")
        self.assertIn("<sup>[1](#ref-1)</sup>", joined)
        self.assertNotIn("^{[1]}", joined)

    def test_formula_graphic_degrade(self):
        """公式为 graphic 形态（无 tex-math/MathML）：占位文本保编号，图 12 个降级。"""
        joined = " ".join(b.get("text", "") for b in self.blocks if b["type"] == "text")
        self.assertIn("[公式 ", joined)

    def test_footnotes_preserved(self):
        """PMC 的 author-notes 通讯邮箱 fn 不再丢弃（page_footnote 块进 PDF 脚注通道）。"""
        fns = [b for b in self.blocks if b["type"] == "page_footnote"]
        self.assertEqual(len(fns), 2)
        joined = " ".join(b["text"] for b in fns)
        self.assertIn("loganlz@zzuli.edu.cn", joined)
        self.assertIn("2015041@zzuli.edu.cn", joined)

    def test_references_count(self):
        self.assertEqual(self.refs["count"], 26)
        self.assertTrue(all(r["raw"].startswith("[") for r in self.refs["references"]))


class TestConvertXmlEndToEnd(unittest.TestCase):
    """端到端（--no-llm）：产物契约与 PDF 路径同构。"""

    @classmethod
    def setUpClass(cls):
        import pipeline
        cls.tmp = Path(tempfile.mkdtemp())
        cls.paper_md = pipeline.convert_xml(
            FIXTURES / "jats_mathml_sample.xml", cls.tmp, use_llm=False)
        if cls.paper_md is None:
            raise AssertionError("convert_xml 失败")

    def test_contract_fields(self):
        text = self.paper_md.read_text(encoding="utf-8")
        self.assertIn("title: Interfacial kinetics", text)
        self.assertIn("doi: 10.9999/jsf.2026.010101", text)
        self.assertIn("date: '2026-01-20'", text)
        self.assertIn("- name: Anna B. Müller", text)
        self.assertIn("abstract: >-", text)
        self.assertIn("container-title: Journal of Synthetic Fixtures", text)
        self.assertNotIn("\r\n", text)  # LF 归一（契约 §四）

    def test_body_structure(self):
        text = self.paper_md.read_text(encoding="utf-8")
        self.assertIn("# Introduction", text)
        self.assertIn("# Experimental", text)
        self.assertIn("## Cell assembly", text)
        self.assertIn("$$", text)
        self.assertIn(r"\tag{1}", text)
        self.assertIn("![Figure 1](images/fig1.png)", text)
        self.assertIn("<table>", text)
        self.assertIn("# References", text)
        self.assertIn('<a id="ref-1"></a>[1]', text)
        # 转跳链接重建：正文引文链到 ref 锚、图链到 fig 锚（锚点由 XML 管线发射）
        self.assertIn("[1](#ref-1)", text)
        self.assertIn("](#fig-1)", text)
        self.assertIn('<a id="fig-1"></a>', text)
        # 排版上下标经 content_processor 通道成行内数学（裸 ^{} 字面量不复存在；
        # ^{ 仅存在于 $$ 展示公式内部，属合法 LaTeX）
        self.assertIn("$_{0}$", text)
        # 图文件实际落盘
        img = self.paper_md.parent / "images" / "fig1.png"
        self.assertTrue(img.exists())
        # source.xml 随产物落盘（重解析据此重走 XML 管线）
        src_xml = self.paper_md.parent / "source.xml"
        self.assertTrue(src_xml.exists())
        self.assertIn("<article", src_xml.read_text(encoding="utf-8")[:2000])

    def test_references_json(self):
        rj = self.paper_md.parent / "references.json"
        self.assertTrue(rj.exists())
        payload = json.loads(rj.read_text(encoding="utf-8"))
        self.assertEqual(payload["source"], "xml")
        self.assertEqual(payload["count"], 4)
        by_n = {r["n"]: r for r in payload["references"]}
        self.assertEqual(by_n[1]["doi"], "10.1016/j.joule.2018.11.011")

    def test_pandoc_ecosystem(self):
        """契约 §五 生态兼容：pandoc 可解析（装了 pandoc 才跑，没装跳过）。"""
        import shutil
        import subprocess
        if not shutil.which("pandoc"):
            self.skipTest("pandoc 未安装（CI 环境跳过）")
        out = self.tmp / "eco.html"
        r = subprocess.run(
            ["pandoc", str(self.paper_md), "-s", "-o", str(out)],
            capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr[:500])
        self.assertTrue(out.exists())


if __name__ == "__main__":
    unittest.main()
