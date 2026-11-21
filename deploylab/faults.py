from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable


INJECTION_KINDS = {
    "model_validation": {
        "truncate_weight": "权重文件在读取过程中被截断",
        "truncate_manifest": "清单读取过程中被截断",
    },
    "quantization": {
        "unknown_quant_dtype": "清单量化类型被替换为未知值",
        "invalid_scale_hint": "缩放口径非法",
        "invalid_zero_point": "零点越界",
    },
    "calibration": {
        "calib_sample_mismatch": "校准样本数与层形状不符",
        "calib_truncated": "校准数组被截断",
    },
    "inference": {
        "input_dim_mismatch": "推理输入维度不符",
    },
}


@dataclass(frozen=True)
class Injection:
    stage: str
    kind: str
    layer_id: str | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Injection":
        stage = str(raw.get("stage", ""))
        kind = str(raw.get("kind", ""))
        if stage not in INJECTION_KINDS:
            raise ValueError(f"unknown injection stage: {stage}")
        if kind not in INJECTION_KINDS[stage]:
            raise ValueError(f"unknown injection kind for {stage}: {kind}")
        layer_id = raw.get("layer_id")
        return cls(stage=stage, kind=kind, layer_id=str(layer_id) if layer_id else None)

    def to_dict(self) -> dict[str, Any]:
        return {"stage": self.stage, "kind": self.kind, "layer_id": self.layer_id}


@dataclass
class FaultInjector:
    injections: list[Injection] = field(default_factory=list)
    crash_stage: str | None = None
    crash_after_layers: int = 0
    crash_marker_path: str | None = None

    @classmethod
    def build(
        cls,
        raw_injections: Iterable[dict[str, Any]] | None = None,
        *,
        crash_stage: str | None = None,
        crash_after_layers: int = 0,
        crash_marker_path: str | None = None,
    ) -> "FaultInjector":
        items = [Injection.from_dict(raw) for raw in (raw_injections or [])]
        if crash_stage is not None and crash_stage not in INJECTION_KINDS:
            raise ValueError(f"unknown crash stage: {crash_stage}")
        return cls(
            injections=items,
            crash_stage=crash_stage,
            crash_after_layers=max(0, int(crash_after_layers)),
            crash_marker_path=crash_marker_path,
        )

    def hit(self, stage: str, kind: str, layer_id: str | None) -> bool:
        for item in self.injections:
            if item.stage != stage or item.kind != kind:
                continue
            if item.layer_id is None or layer_id is None or item.layer_id == layer_id:
                return True
        return False

    def for_stage(self, stage: str) -> list[Injection]:
        return [item for item in self.injections if item.stage == stage]

    def to_dict(self) -> dict[str, Any]:
        return {
            "injections": [item.to_dict() for item in self.injections],
            "crash_stage": self.crash_stage,
            "crash_after_layers": self.crash_after_layers,
        }

