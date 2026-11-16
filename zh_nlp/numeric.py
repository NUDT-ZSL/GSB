"""数值稳定性与确定性 IO 工具。"""
from __future__ import annotations

import json
import math
import os
import tempfile
from pathlib import Path

import numpy as np

NEG_INF = float("-inf")
SCORE_DECIMALS = 6


def round_score(value: float) -> float:
    """对数得分统一保留 6 位小数，保证跨运行逐字节一致。"""
    if value == NEG_INF or math.isnan(value):
        return value
    rounded = round(float(value), SCORE_DECIMALS)
    return 0.0 if rounded == 0.0 else rounded


def stable_log_sum_exp(log_values: np.ndarray) -> float:
    """减去最大值的稳定 log-sum-exp，不做任何近似截断。"""
    if log_values.size == 0:
        return NEG_INF
    finite = log_values[np.isfinite(log_values)]
    if finite.size == 0:
        return NEG_INF
    maximum = float(np.max(finite))
    total = np.sum(np.exp(log_values - maximum))
    return float(maximum + math.log(total))


def log_safe(probability: float) -> float:
    """概率转自然对数；零概率记为负无穷。"""
    if probability <= 0.0:
        return NEG_INF
    return math.log(probability)


class DeterministicJson:
    """固定序列化选项：sort_keys + 不转义中文 + 统一缩进与换行。"""

    @staticmethod
    def dumps(obj) -> str:
        return json.dumps(
            obj,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            separators=(",", ": "),
            allow_nan=False,
        )

    @staticmethod
    def atomic_write(path: str | Path, obj) -> None:
        """临时文件 + os.replace 原子替换，保证中断后不会出现半截文件。"""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(target.parent),
            prefix=target.name + ".",
            suffix=".tmp",
            delete=False,
            newline="\n",
        )
        try:
            handle.write(DeterministicJson.dumps(obj))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            handle.close()
            os.replace(handle.name, target)
        except BaseException:
            handle.close()
            if os.path.exists(handle.name):
                os.unlink(handle.name)
            raise
