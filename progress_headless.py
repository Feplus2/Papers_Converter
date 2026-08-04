r"""
无界面进度报告器 — 供 SageRead sidecar 集成使用。
向 stdout 逐行打印 JSON（ensure_ascii=True，每行立即 flush），普通日志仍走 stderr。

事件形状（与 Books_Converter headless 协议对齐）：
    {"type":"start","title","engine"}
    {"type":"progress","stage","stage_name","detail","fraction","percent"}
    {"type":"stage_done","stage","stage_name","elapsed","percent"}
    {"type":"done",...,"percent":100}
    {"type":"error","message"}

percent 为 0-100 整数、全程单调不减：provider 的 fraction 映射进当前
阶段区间（stage 1 OCR 解析占 0-70%，stage 2/3/4 各占 10%），只前进不后退。
"""

import json
import sys
import threading
import time

# 阶段编号（SageRead 侧展示）：1=OCR 解析 2=元数据提取 3=内容处理 4=渲染装订
_STAGE_SPAN = {1: (0, 70), 2: (70, 80), 3: (80, 90), 4: (90, 100)}

_emit_lock = threading.Lock()


def _emit(obj: dict):
    try:
        with _emit_lock:
            sys.stdout.write(json.dumps(obj, ensure_ascii=True) + "\n")
            sys.stdout.flush()
    except Exception:
        pass


def emit_error(message: str):
    _emit({"type": "error", "message": message})


class HeadlessProgress:
    """单篇转换的 headless 进度报告器（无状态、无后台线程）。"""

    def __init__(self, title: str, engine: str = ""):
        self._title = title
        self._engine = engine
        self._t0 = time.time()
        self._percent = 0  # 对外 percent，单调不减

    def _advance(self, value: float) -> int:
        self._percent = max(self._percent, int(round(value)))
        return self._percent

    def start(self):
        _emit({"type": "start", "title": self._title, "engine": self._engine})

    def update_stage(self, stage: int, stage_name: str, detail: str = "",
                     fraction: float | None = None):
        if fraction is not None:
            base, ceiling = _STAGE_SPAN.get(stage, (0, 100))
            frac = max(0.0, min(float(fraction), 1.0))
            self._advance(base + frac * (ceiling - base))
        _emit({"type": "progress", "stage": stage, "stage_name": stage_name,
               "detail": detail, "fraction": fraction, "percent": self._percent})

    def complete_stage(self, stage: int, stage_name: str, elapsed: float):
        _base, ceiling = _STAGE_SPAN.get(stage, (0, 100))
        self._advance(ceiling)
        _emit({"type": "stage_done", "stage": stage, "stage_name": stage_name,
               "elapsed": round(elapsed, 1), "percent": self._percent})

    def finish(self, **fields):
        self._percent = 100
        _emit({"type": "done", **fields,
               "elapsed": round(time.time() - self._t0, 1), "percent": 100})
