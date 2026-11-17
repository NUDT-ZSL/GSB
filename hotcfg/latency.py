"""推送与查询交互延迟观测。

时钟基准: time.monotonic (本地单调计数, 不受系统时钟回拨影响)。
真值来源: 本地单调计数器, 每次交互记录起始与结束毫秒差。
统计口径: 整数定点表示 (毫秒 * SCALE_MS), 量化误差上界 0.5/SCALE_MS 毫秒。
"""
from __future__ import annotations

import math
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, asdict

from .errors import BizError, E_SAMPLE

SCALE_MS = 1000
MIN_SAMPLES = 3
STATS_SPEC_VERSION = "latency-stats-v1"
CLOCK_BASIS = "time.monotonic"
TRUTH_SOURCE = "local-monotonic-counter"
QUANT_ERROR_BOUND_MS = 0.5 / SCALE_MS

KIND_PUSH = "push"
KIND_QUERY = "query"


@dataclass(frozen=True)
class LatencySample:
    task_id: str
    version: int
    kind: str
    start_ms: float
    end_ms: float

    @property
    def duration_ms(self) -> float:
        return self.end_ms - self.start_ms


class LatencyObserver:
    """记录交互延迟样本并按 (任务, 版本, 类型) 聚合统计。"""

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._samples: list[LatencySample] = []
        self._lock = threading.Lock()

    def now_ms(self) -> float:
        return self._clock() * 1000.0

    @contextmanager
    def measure(self, task_id: str, version: int, kind: str):
        start = self.now_ms()
        try:
            yield
        finally:
            self.record(task_id, version, kind, start, self.now_ms())

    def record(self, task_id: str, version: int, kind: str,
               start_ms: float, end_ms: float) -> None:
        with self._lock:
            self._samples.append(LatencySample(task_id, version, kind, start_ms, end_ms))

    @property
    def samples(self) -> list[LatencySample]:
        with self._lock:
            return list(self._samples)

    def load_samples(self, samples: list[dict]) -> None:
        with self._lock:
            self._samples = [LatencySample(**s) for s in samples]

    def stats(self, task_id=None, version=None, kind=None, group_version=True) -> dict:
        """按 (任务, 版本, 类型) 聚合, 返回整数定点统计。

        任一分组样本数不足 MIN_SAMPLES 时抛出 E_SAMPLE,
        不以缺失值参与统计。
        """
        groups: dict[tuple, list[int]] = {}
        for s in self.samples:
            if task_id is not None and s.task_id != task_id:
                continue
            if version is not None and s.version != version:
                continue
            if kind is not None and s.kind != kind:
                continue
            key = (s.task_id, s.version if group_version else None, s.kind)
            fp = int(round(s.duration_ms * SCALE_MS))
            groups.setdefault(key, []).append(fp)
        if not groups:
            raise BizError(E_SAMPLE, "no latency samples for requested filter",
                           {"task_id": task_id, "version": version, "kind": kind})
        insufficient = [
            {"group": list(key), "count": len(ds), "min_samples": MIN_SAMPLES}
            for key, ds in groups.items() if len(ds) < MIN_SAMPLES
        ]
        if insufficient:
            raise BizError(E_SAMPLE, "insufficient latency samples",
                           {"groups": insufficient, "min_samples": MIN_SAMPLES})
        out = {}
        for key, ds in sorted(groups.items()):
            ds.sort()
            n = len(ds)
            out[key] = {
                "count": n,
                "mean": int(round(sum(ds) / n)),
                "p95": ds[math.ceil(0.95 * n) - 1],
                "max": ds[-1],
                "unit": "fixed-point-ms",
                "scale": SCALE_MS,
                "quant_error_bound_ms": QUANT_ERROR_BOUND_MS,
                "clock_basis": CLOCK_BASIS,
                "truth_source": TRUTH_SOURCE,
                "stats_spec_version": STATS_SPEC_VERSION,
            }
        return out

    def stats_json(self, **kwargs) -> dict:
        """可 JSON 序列化的统计结果, 键为 task|version|kind。"""
        return {
            "|".join("-" if part is None else str(part) for part in key): value
            for key, value in self.stats(**kwargs).items()
        }

    def samples_json(self) -> list[dict]:
        return [asdict(s) for s in self.samples]
