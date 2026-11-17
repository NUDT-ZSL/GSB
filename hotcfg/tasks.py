"""计算任务: 按指定版本的完整生效值对确定性样本数据执行聚合。"""
from __future__ import annotations

import numpy as np

from .errors import BizError, E_VERSION
from .loader import TaskSpec

RESULT_SCALE = 1_000_000  # 结果整数定点比例因子


class ComputeTask:
    """本地计算任务。结果摘要与所引用配置版本严格对应。"""

    def __init__(self, spec: TaskSpec, series: dict[str, list[float]]):
        self.spec = spec
        self._series = {}
        for key in spec.keys:
            if key not in series:
                raise BizError(E_VERSION, f"no sample series for key {key}",
                               {"task_id": spec.id, "key": key})
            arr = np.asarray(series[key], dtype=np.float64)
            self._series[key] = {
                "sum": float(np.sum(arr)),
                "mean": float(np.mean(arr)),
                "max": float(np.max(arr)),
                "min": float(np.min(arr)),
                "n": int(arr.size),
            }
        self.last_notification: dict | None = None

    def on_push(self, center, notification: dict) -> None:
        """收到推送后以一次原子操作取到该版本完整生效值集合。"""
        effective = center.get_effective(notification["version"])
        self.last_notification = {**notification, "effective": effective}

    def compute_digest(self, effective: dict, version: int) -> dict:
        keys = sorted(self.spec.keys)
        vals = np.array([effective[k]["value"] for k in keys], dtype=np.float64)
        weights = np.array([effective[k]["weight"] for k in keys], dtype=np.float64)
        sums = np.array([self._series[k]["sum"] for k in keys], dtype=np.float64)
        means = np.array([self._series[k]["mean"] for k in keys], dtype=np.float64)
        maxs = np.array([self._series[k]["max"] for k in keys], dtype=np.float64)
        mins = np.array([self._series[k]["min"] for k in keys], dtype=np.float64)
        agg = self.spec.aggregation
        if agg == "sum":
            result = float(np.sum(vals * sums))
        elif agg == "weighted_sum":
            result = float(np.sum(weights * vals * sums))
        elif agg == "avg":
            result = float(np.mean(vals * means))
        elif agg == "max":
            result = float(np.max(vals * maxs))
        elif agg == "min":
            result = float(np.min(vals * mins))
        else:  # pragma: no cover - 加载期已校验
            raise BizError(E_VERSION, f"unsupported aggregation: {agg}")
        return {
            "task_id": self.spec.id,
            "version": version,
            "aggregation": agg,
            "keys": keys,
            "inputs": {
                k: {"value": effective[k]["value"], "weight": effective[k]["weight"]}
                for k in keys
            },
            "value_fp": int(round(result * RESULT_SCALE)),
            "scale": RESULT_SCALE,
        }

    def run(self, center, version: int) -> dict:
        """按指定版本取生效值并计算。版本与订阅声明不一致时以 E_VERSION 拒绝。"""
        expected = self.spec.expected_version
        if expected is not None and version != expected:
            raise BizError(
                E_VERSION,
                f"task {self.spec.id} declared version {expected} but was asked to run at {version}",
                {"task_id": self.spec.id, "expected_version": expected,
                 "requested_version": version},
            )
        effective = center.query(self.spec.id, version, keys=self.spec.keys)
        return self.compute_digest(effective, version)
