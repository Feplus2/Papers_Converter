# 封面判定 AB 对照：旧规则 vs 新规则在真实语料上的全量对比。
#
# 用法:
#   python ab_cover_detect.py [parsed_dir] [--extra GLOB ...]
#   parsed_dir：解析缓存目录（content_list.json 所在），需显式传入
#   --extra 追加 content_list 通配（如事故 staging：'.tmp-qc-gate-run\_staging\*\*_content_list.json'）
#
# 输出：对照表（旧判定 / 新判定 / 差异标注）+ 汇总。
# 新规则不传权威标题（纯规则对比；锚定否决只会更保守，不影响误杀面评估）。
# AB 结果追加记录见 docs/structure-detection.md。

import argparse
import glob
import json
import logging
import sys
from pathlib import Path

logging.getLogger("cover_detect").setLevel(logging.ERROR)  # 批量跑时静音逐篇判定日志

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cover_detect import (  # noqa: E402
    COVER_PAGE_MARKERS,
    detect_cover_pages,
    legacy_detect_cover_pages,
)

_NOISE = {"header", "footer", "page_number", "aside_text"}


def page_features(content_list: list[dict], pidx: int) -> str:
    """页面特征摘要（人工抽查用）"""
    blocks = [b for b in content_list if b.get("page_idx", 0) == pidx]
    texts = [(b.get("text") or "") for b in blocks if b.get("type") not in _NOISE]
    prose = sum(len(t) for t in texts)
    rich = sorted({b.get("type") for b in blocks
                   if b.get("type") in ("image", "equation", "table")})
    low = " ".join(texts).lower()
    hits = [m for m in COVER_PAGE_MARKERS if m in low]
    return f"标记{len(hits)} prose={prose} rich={rich or '-'}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("parsed_dir", nargs="?", default="parsed",
                    help="解析缓存目录（含 *_content_list.json）")
    ap.add_argument("--extra", action="append", default=[],
                    help="追加 content_list 通配")
    args = ap.parse_args()

    files = sorted(Path(args.parsed_dir).glob("*/*_content_list.json"))
    entries = [(p.parent.name, p) for p in files]
    for pat in args.extra:
        for p in sorted(Path().glob(pat)) if not Path(pat).is_absolute() \
                else sorted(glob.glob(pat)):
            entries.append((Path(p).stem[:30], Path(p)))

    total = loaded = 0
    old_hits = new_hits = 0
    rows = []
    for name, path in entries:
        try:
            data = json.load(open(path, encoding="utf-8"))
        except Exception as e:
            print(f"  跳过 {name}: {e}", file=sys.stderr)
            continue
        loaded += 1
        old = legacy_detect_cover_pages(data)
        new = detect_cover_pages(data, use_llm=False)  # 纯规则对照，不走 LLM
        total += 1
        old_hits += len(old)
        new_hits += len(new)
        if old or new:
            diff = "一致" if old == new else (
                "旧杀新留" if old and not new else
                "新杀旧留" if new and not old else "页面不同")
            rows.append((name, sorted(old), sorted(new), diff,
                         page_features(data, 0)))
        if old != new:
            for p in sorted(old - new):
                print(f"  [旧杀新留] {name} page{p} {page_features(data, p)}")
            for p in sorted(new - old):
                print(f"  [新杀旧留] {name} page{p} {page_features(data, p)}")

    print(f"\n扫描 {loaded}/{total} 篇")
    print(f"旧规则切页总数: {old_hits}，新规则切页总数: {new_hits}")
    print(f"\n{'篇目':<22} {'旧判定':<10} {'新判定':<10} {'差异':<10} page0 特征")
    for name, old, new, diff, feat in rows:
        print(f"{name:<22} {str(old):<10} {str(new):<10} {diff:<10} {feat}")


if __name__ == "__main__":
    main()
