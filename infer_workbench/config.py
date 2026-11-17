"""Batch configuration parsing, validation, and deterministic versioning."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .canonical import digest_object
from .errors import WorkbenchError


SORT_STRATEGIES = ("sample_id", "input_norm", "input_order")
PRECISION_MODES = ("float32", "fixed")
DEFAULT_DRIFT_THRESHOLD = 0.01


def _is_plain_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def power_of_ten(value: Any) -> int:
    if not _is_plain_int(value) or value <= 0:
        raise WorkbenchError(
            "E_CONFIG", "量化缩放因子必须为正整数且为 10 的整数幂", {"field": "quant_scale"}
        )
    power = 0
    current = 1
    while current < value:
        current *= 10
        power += 1
    if current != value:
        raise WorkbenchError(
            "E_CONFIG", "量化缩放因子必须为 10 的整数幂", {"quant_scale": value}
        )
    return power


@dataclass(frozen=True)
class BatchConfig:
    batch_size: int
    sort_strategy: str
    precision_mode: str
    quant_scale: int
    quant_scale_power: int
    drift_threshold: float = DEFAULT_DRIFT_THRESHOLD

    @classmethod
    def from_dict(cls, raw: Any) -> "BatchConfig":
        if not isinstance(raw, dict):
            raise WorkbenchError("E_CONFIG", "配置顶层必须为 JSON 对象")

        batch_size = raw.get("batch_size")
        if not _is_plain_int(batch_size) or batch_size <= 0:
            raise WorkbenchError(
                "E_CONFIG", "批大小必须为正整数", {"field": "batch_size", "value": batch_size}
            )

        sort_strategy = raw.get("sort_strategy")
        if sort_strategy not in SORT_STRATEGIES:
            raise WorkbenchError(
                "E_CONFIG",
                "批内排序策略不受支持",
                {"field": "sort_strategy", "value": sort_strategy, "supported": list(SORT_STRATEGIES)},
            )

        precision_mode = raw.get("precision_mode")
        if precision_mode not in PRECISION_MODES:
            raise WorkbenchError(
                "E_CONFIG",
                "精度模式不受支持",
                {"field": "precision_mode", "value": precision_mode, "supported": list(PRECISION_MODES)},
            )

        scale_present = "quant_scale" in raw
        power_present = "quant_scale_power" in raw
        if not scale_present and not power_present:
            raise WorkbenchError("E_CONFIG", "缺少量化缩放因子", {"field": "quant_scale"})
        if power_present:
            power_value = raw.get("quant_scale_power")
            if not _is_plain_int(power_value) or power_value < 0:
                raise WorkbenchError(
                    "E_CONFIG",
                    "quant_scale_power 必须为非负整数",
                    {"field": "quant_scale_power", "value": power_value},
                )
            derived_scale = 10**power_value
            if scale_present and raw.get("quant_scale") != derived_scale:
                raise WorkbenchError(
                    "E_CONFIG",
                    "quant_scale 与 quant_scale_power 不一致",
                    {
                        "quant_scale": raw.get("quant_scale"),
                        "quant_scale_power": power_value,
                    },
                )
            quant_scale = derived_scale
            quant_power = int(power_value)
        else:
            quant_scale = raw.get("quant_scale")
            quant_power = power_of_ten(quant_scale)

        threshold = raw.get("drift_threshold", DEFAULT_DRIFT_THRESHOLD)
        if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
            raise WorkbenchError(
                "E_CONFIG", "偏差阈值必须为非负数", {"field": "drift_threshold", "value": threshold}
            )
        threshold_float = float(threshold)
        if not math.isfinite(threshold_float) or threshold_float < 0:
            raise WorkbenchError(
                "E_CONFIG", "偏差阈值必须为有限非负数", {"field": "drift_threshold"}
            )

        return cls(
            batch_size=int(batch_size),
            sort_strategy=str(sort_strategy),
            precision_mode=str(precision_mode),
            quant_scale=int(quant_scale),
            quant_scale_power=int(quant_power),
            drift_threshold=threshold_float,
        )

    @classmethod
    def load(cls, path: str | Path) -> "BatchConfig":
        source = Path(path)
        try:
            with source.open("r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except OSError as exc:
            raise WorkbenchError(
                "E_CONFIG", f"无法读取批处理配置: {source}", {"path": source.as_posix()}
            ) from exc
        except json.JSONDecodeError as exc:
            raise WorkbenchError(
                "E_CONFIG",
                f"批处理配置 JSON 解析失败: {source}",
                {"path": source.as_posix(), "line": exc.lineno, "column": exc.colno},
            ) from exc
        return cls.from_dict(raw)

    def version_payload(self) -> dict[str, Any]:
        return {
            "batch_size": self.batch_size,
            "sort_strategy": self.sort_strategy,
            "precision_mode": self.precision_mode,
            "quant_scale": self.quant_scale,
            "quant_scale_power": self.quant_scale_power,
            "drift_threshold": self.drift_threshold,
        }

    @property
    def config_version(self) -> str:
        return digest_object(self.version_payload())[:16]

    def to_dict(self) -> dict[str, Any]:
        value = self.version_payload()
        value["config_version"] = self.config_version
        return value

    def changed_fields(self, other: "BatchConfig") -> list[str]:
        left = self.version_payload()
        right = other.version_payload()
        return [field for field in sorted(left) if left[field] != right[field]]
