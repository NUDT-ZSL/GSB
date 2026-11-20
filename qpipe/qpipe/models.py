"""Typed data models shared by every stage of the pipeline."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .deterministic import QUANT_RANGES
from .errors import E_PACKAGE_FORMAT, E_ZP_OUT_OF_RANGE, PipelineError

REQUIRED_FIELDS = (
    "model_tag",
    "layer_id",
    "layer_index",
    "layer_shape",
    "weight_revision",
    "quant_dtype",
    "zero_point",
    "scale_hint",
    "calibration_split",
)


@dataclass(frozen=True)
class LayerSpec:
    """One manifest row describing a single MLP layer."""

    model_tag: str
    layer_id: str
    layer_index: int
    layer_shape: Tuple[int, int]
    weight_revision: int
    quant_dtype: str
    zero_point: int
    scale_hint: float
    calibration_split: int

    @property
    def in_features(self) -> int:
        return int(self.layer_shape[0])

    @property
    def out_features(self) -> int:
        return int(self.layer_shape[1])

    def numel(self) -> int:
        return int(self.layer_shape[0]) * int(self.layer_shape[1])

    def quant_range(self) -> Tuple[int, int]:
        return QUANT_RANGES[self.quant_dtype]

    def to_manifest_dict(self) -> Dict[str, Any]:
        return {
            "model_tag": self.model_tag,
            "layer_id": self.layer_id,
            "layer_index": self.layer_index,
            "layer_shape": list(self.layer_shape),
            "weight_revision": self.weight_revision,
            "quant_dtype": self.quant_dtype,
            "zero_point": self.zero_point,
            "scale_hint": self.scale_hint,
            "calibration_split": self.calibration_split,
        }

    @staticmethod
    def from_manifest_dict(row: Dict[str, Any]) -> "LayerSpec":
        missing = [name for name in REQUIRED_FIELDS if name not in row]
        if missing:
            raise PipelineError(
                E_PACKAGE_FORMAT,
                f"missing required fields: {','.join(missing)}",
                stage="validate",
                model_tag=row.get("model_tag"),
                layer_id=row.get("layer_id"),
                detail={"missing": missing},
            )
        shape = tuple(int(v) for v in row["layer_shape"])
        spec = LayerSpec(
            model_tag=str(row["model_tag"]),
            layer_id=str(row["layer_id"]),
            layer_index=int(row["layer_index"]),
            layer_shape=shape,
            weight_revision=int(row["weight_revision"]),
            quant_dtype=str(row["quant_dtype"]),
            zero_point=int(row["zero_point"]),
            scale_hint=float(row["scale_hint"]),
            calibration_split=int(row["calibration_split"]),
        )
        if spec.quant_dtype not in QUANT_RANGES:
            raise PipelineError(
                E_PACKAGE_FORMAT,
                f"unknown quant_dtype {spec.quant_dtype!r}",
                stage="validate",
                model_tag=spec.model_tag,
                layer_id=spec.layer_id,
                layer_index=spec.layer_index,
            )
        lo, hi = QUANT_RANGES[spec.quant_dtype]
        if not (lo <= spec.zero_point <= hi):
            raise PipelineError(
                E_ZP_OUT_OF_RANGE,
                f"zero_point {spec.zero_point} not in [{lo},{hi}]",
                stage="validate",
                model_tag=spec.model_tag,
                layer_id=spec.layer_id,
                layer_index=spec.layer_index,
            )
        return spec


@dataclass(frozen=True)
class ModelPackage:
    """A validated, ordered collection of :class:`LayerSpec` rows."""

    model_tag: str
    layers: Tuple[LayerSpec, ...]

    @property
    def layer_count(self) -> int:
        return len(self.layers)

    def indices(self) -> List[int]:
        return [layer.layer_index for layer in self.layers]

    def downstream_of(self, layer_index: int) -> List[int]:
        """The changed layer plus every downstream layer (dependency fan-out)."""
        return [
            layer.layer_index
            for layer in self.layers
            if layer.layer_index >= layer_index
        ]


@dataclass
class ResourceBudget:
    """Configurable hard limits / streaming thresholds.

    These are *budget observations and guards*, not real isolation: memory is
    estimated from layer shapes and quant widths.
    """

    max_layers_per_stage: int = 64
    max_resident_dequant_bytes: int = 8 * 1024 * 1024
    max_cache_entries: int = 1024
    stream_dequant_bytes: int = 2 * 1024 * 1024

    @staticmethod
    def from_dict(data: Optional[Dict[str, Any]]) -> "ResourceBudget":
        if not data:
            return ResourceBudget()
        return ResourceBudget(
            max_layers_per_stage=int(
                data.get("max_layers_per_stage", 64)
            ),
            max_resident_dequant_bytes=int(
                data.get("max_resident_dequant_bytes", 8 * 1024 * 1024)
            ),
            max_cache_entries=int(data.get("max_cache_entries", 1024)),
            stream_dequant_bytes=int(
                data.get("stream_dequant_bytes", 2 * 1024 * 1024)
            ),
        )

    def to_dict(self) -> Dict[str, int]:
        return asdict(self)


@dataclass
class Degradation:
    """Marker attached to a layer result when a degraded path was taken."""

    category: str
    trigger_code: str
    stage: str
    layer_index: int
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class FaultRecord:
    """A single injected-fault attribution record."""

    stage: str
    code: str
    layer_index: Optional[int]
    layer_id: Optional[str]
    message: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class RunContext:
    """Per-run switches: fault injection and kill coordination.

    Everything here is supplied by input/parameters (a JSON file or CLI args);
    source code is never modified to trigger a fault.
    """

    faults: List[Dict[str, Any]] = field(default_factory=list)
    kill_after_layer: Optional[int] = None
    kill_stage: Optional[str] = None
    seed: int = 20261121

    @staticmethod
    def from_dict(data: Optional[Dict[str, Any]]) -> "RunContext":
        data = data or {}
        return RunContext(
            faults=list(data.get("faults", [])),
            kill_after_layer=data.get("kill_after_layer"),
            kill_stage=data.get("kill_stage"),
            seed=int(data.get("seed", 20261121)),
        )

    def faults_for(self, stage: str) -> List[Dict[str, Any]]:
        return [f for f in self.faults if f.get("stage") == stage]
