r"""解析产物退化检测（签名周期法）。

背景：stage1 OCR 引擎（PaddleOCR-VL 类 VLM）在长枚举内容上偶发"模式延续"
失控——真实事故：LED 波长列从真实的 1700 nm 被一路编造递增到 15800 nm；
另一篇单词 fire 重复数百次。失控在引擎原始产物就存在，精确重复检测抓不到
数字递增情形。

算法（SageRead 侧 packages/app/src/utils/degenerate.ts 的 Python 移植，
阈值经对侧实测调定，勿放宽）：
- 按行扫描，行长 < 200 的行不查（宽表分隔行、编号列表天然豁免）
- 签名映射：Unicode 字母→a、数字→0、连续空白折叠为单空格
  （"1700 nm, " → "0000 aa, "，数字递增与精确重复在签名层同构）
- 起点按 10 步进抽样，周期 4..50：某起点处同一签名周期连续重复 ≥10 次
  且覆盖 ≥300 字符即判定退化，返回首个命中
- "|---|---|" 这类表格分隔行周期短（4）但跨度/次数有限（60 列仅 240 字符
  < 300），不会误中；正常论文正文无此长周期重复
"""

import json
import logging
import re
from pathlib import Path
from typing import NamedTuple

logger = logging.getLogger(__name__)

MIN_LINE_LEN = 200   # 行长下限：短行不查
MIN_SPAN = 300       # 重复覆盖跨度下限（字符数）
MIN_REPEATS = 10     # 连续重复次数下限
MAX_PERIOD = 50      # 周期上限（签名层字符数）
MIN_PERIOD = 4       # 周期下限
START_STEP = 10      # 起点抽样步进
PREVIEW_LEN = 120    # 命中预览截断长度

# stage1 命中退化后的最大重试次数（VLM 失控是随机的，重跑常能自愈）
MAX_STAGE1_RETRIES = 2

_LETTER_RE = re.compile(r"[^\W\d_]")  # Unicode 字母（\w 去掉数字与下划线）
_DIGIT_RE = re.compile(r"\d")
_WS_RE = re.compile(r"\s+")


class DegenerateFinding(NamedTuple):
    """首个命中证据。line_no 为 0 基行号（与对侧 TS 实现一致）。"""
    line_no: int
    period: int
    repeats: int
    preview: str


def _to_signature(s: str) -> str:
    s = _LETTER_RE.sub("a", s)
    s = _DIGIT_RE.sub("0", s)
    return _WS_RE.sub(" ", s)


def _count_repeats(sig: str, start: int, p: int) -> int:
    """在签名的 start 处，周期 p 连续重复的次数（从 start 起算，至少 1）。"""
    unit = sig[start:start + p]
    count = 1
    pos = start + p
    n = len(sig)
    while pos + p <= n and sig[pos:pos + p] == unit:
        count += 1
        pos += p
    return count


def find_degenerate_loop(body: str) -> DegenerateFinding | None:
    """检测正文中的退化循环，返回首个命中；无命中返回 None。

    只查单行：行长 ≥200、周期 4..50、连续重复 ≥10 次、覆盖 ≥300 字符才判定。
    """
    for line_no, line in enumerate(body.split("\n")):
        if len(line) < MIN_LINE_LEN:
            continue
        sig = _to_signature(line)
        # 步进抽样起点，命中即可，不需要穷举
        for start in range(0, len(sig) - MAX_PERIOD * MIN_REPEATS + 1, START_STEP):
            for p in range(MIN_PERIOD, MAX_PERIOD + 1):
                repeats = _count_repeats(sig, start, p)
                if repeats >= MIN_REPEATS and repeats * p >= MIN_SPAN:
                    return DegenerateFinding(
                        line_no=line_no,
                        period=p,
                        repeats=repeats,
                        preview=line[start:start + min(PREVIEW_LEN, repeats * p)],
                    )
    return None


def check_staging_dir(staging_dir: str | Path) -> DegenerateFinding | None:
    """检测 stage1 解析产物目录（引擎原始产物）是否退化。

    扫描 {stem}.md（引擎直出 markdown，失控在原始产物即存在，最先暴露），
    无 md 时退而扫描 *_content_list.json 的文本字段（下游实际消费的内容）。
    """
    staging_dir = Path(staging_dir)
    for md in sorted(staging_dir.glob("*.md")):
        try:
            finding = find_degenerate_loop(
                md.read_text(encoding="utf-8", errors="replace"))
        except OSError as e:
            logger.warning(f"  退化检测读取失败（跳过）: {md}: {e}")
            continue
        if finding:
            return finding
    for cl in sorted(staging_dir.glob("*_content_list.json")):
        try:
            blocks = json.loads(cl.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"  退化检测读取失败（跳过）: {cl}: {e}")
            continue
        text = _content_list_text(blocks)
        if text:
            finding = find_degenerate_loop(text)
            if finding:
                return finding
    return None


def _content_list_text(blocks: list) -> str:
    """拼接 content_list 各块的文本字段（每块一行）。"""
    lines = []
    for b in blocks:
        if not isinstance(b, dict):
            continue
        for key in ("text", "table_body"):
            s = b.get(key)
            if s:
                lines.append(str(s).replace("\n", " "))
        for key in ("image_caption", "table_caption"):
            for s in b.get(key) or []:
                if s:
                    lines.append(str(s).replace("\n", " "))
    return "\n".join(lines)


def describe(finding: DegenerateFinding) -> str:
    """人类可读的命中描述（日志/警告用）。"""
    return (f"行 {finding.line_no}，签名周期 {finding.period} "
            f"连续重复 {finding.repeats} 次，预览: {finding.preview[:60]!r}")
