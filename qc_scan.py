# 产物质量扫描器（临时工具）：对 output/ 全量做机械异常检查
# 用法: .venv/Scripts/python _qc_scan.py [output_dir]
import json
import re
import sys
from pathlib import Path

out = Path(sys.argv[1] if len(sys.argv) > 1 else "output")
parsed = Path(r"F:\MyProjects\zotero-brain\parsed")

# 源数据索引：zotero_key -> 统计
src = {}
for d in parsed.iterdir():
    if not d.is_dir():
        continue
    cls = list(d.glob("*_content_list.json"))
    if not cls:
        continue
    try:
        data = json.load(open(cls[0], encoding="utf-8"))
    except Exception:
        continue
    src[d.name] = {
        "tables": sum(1 for b in data if b.get("type") == "table"
                      and (b.get("table_body") or "").strip()),
        "eqs": sum(1 for b in data if b.get("type") == "equation"),
    }

# slug 目录 -> zotero_key（从 frontmatter 读）
issues = []
n_files = 0
for paper in sorted(out.iterdir()):
    md = paper / "paper.md"
    if not paper.is_dir() or not md.exists():
        continue
    n_files += 1
    text = md.read_text(encoding="utf-8")
    name = paper.name

    fm = {}
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if m:
        for line in m.group(1).splitlines():
            mm = re.match(r"^([a-zA-Z_-]+):\s*(.*)$", line)
            if mm:
                fm[mm.group(1)] = mm.group(2).strip().strip("'\"")
    zkey = fm.get("zotero_key", "")

    # 1) 必填字段
    if not fm.get("abstract"):
        issues.append((name, "frontmatter abstract 为空"))
    if not fm.get("date"):
        issues.append((name, "frontmatter date 为空"))
    if not fm.get("container-title"):
        issues.append((name, "container-title 缺失"))

    # 2) 表格数量对账（源有表体的表格应全部出现）
    n_tbl_out = text.count("<table>")
    n_tbl_src = src.get(zkey, {}).get("tables")
    if n_tbl_src is not None and n_tbl_out < n_tbl_src:
        issues.append((name, f"表格缺失: 源 {n_tbl_src} 产出 {n_tbl_out}"))

    # 3) 图片重名 & 残留标签 & 行尾
    imgs = list((paper / "images").glob("*")) if (paper / "images").exists() else []
    if len({f.name for f in imgs}) != len(imgs):
        issues.append((name, "images 重名"))
    if "<sup>" in text or "<sub>" in text:
        issues.append((name, "残留 sup/sub 标签"))
    if "\r" in text:
        issues.append((name, "存在 CR 字符"))
    if "$^{\\*}" in text:
        issues.append((name, "非法脚注上标 $^{\\*}$"))

    # 4) 结构 sanity：H1 过多（>12 疑似压平/编号异常）、无 H1
    h1 = len(re.findall(r"^# ", text, re.M))
    if h1 == 0:
        issues.append((name, "无 H1"))
    elif h1 > 12:
        issues.append((name, f"H1 过多 ({h1})"))

print(f"扫描 {n_files} 篇")
print(f"异常 {len(issues)} 条")
from collections import Counter
c = Counter(i[1].split(":")[0] for i in issues)
for k, v in c.most_common():
    print(f"  {k}: {v}")
for name, msg in issues[:60]:
    print(f"    {name}: {msg}")
