# 受损论文重转验证：复用已有解析缓存（不调 OCR API），验证封面根修后产物完整。
# 输出到 .tmp-reconvert/，逐篇核对页锚/图表/QC 严重级。
import logging
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%H:%M:%S")

from pipeline import convert_single  # noqa: E402
from qc_paper import qc_paper_md, qc_severe_findings  # noqa: E402

OUT = Path(r"F:\MyProjects\Papers_Converter\.tmp-reconvert")
if OUT.exists():
    shutil.rmtree(OUT)
OUT.mkdir(parents=True)

CASES = [
    # (名称, parsed_dir, zotero_key, pdf_pages)
    ("zhao2020(Science 5页综述)",
     next(d for d in Path(r"F:\MyProjects\Papers_Converter\.tmp-qc-gate-run\_staging").iterdir()
          if d.is_dir() and list(d.glob("*_content_list.json"))),
     None, 5),
    ("D9AVT22J", Path(r"F:\MyProjects\zotero-brain\parsed\D9AVT22J"), "D9AVT22J", None),
    ("L2KRJ8KZ", Path(r"F:\MyProjects\zotero-brain\parsed\L2KRJ8KZ"), "L2KRJ8KZ", None),
]

for name, parsed, zkey, pages in CASES:
    print(f"\n===== {name} =====")
    md = convert_single(parsed, OUT, use_llm=False, zotero_key=zkey)
    if not md:
        print(f"  转换失败: {name}")
        continue
    text = md.read_text(encoding="utf-8")
    marks = [int(m.group(1)) for m in re.finditer(r"<!--\s*page:\s*(\d+)\s*-->", text)]
    figs = sorted({int(m.group(1) or m.group(2))
                   for m in re.finditer(
                       r"^!\[(?:Figure|Fig\.?)\s*(\d+)[\w.\-]*|^Figure\s*(\d+)\s*[:.]", text, re.M)})
    severe = qc_severe_findings(md, pages)
    warns = qc_paper_md(md)
    print(f"  产物: {md}")
    print(f"  字符数: {len(text)}  页锚: {marks}  图编号: {figs or '无'}")
    print(f"  QC 严重级: {severe or '无'}")
