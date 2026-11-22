from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from .errors import BusinessError, E_INVALID_CONFIG


DEFAULT_STAGE_NAMES = (
    "model_validation",
    "quantization",
    "calibration",
    "inference",
)


@dataclass
class WorkbenchConfig:
    seed: int = 20261123
    model_tag: str = "local-llm-demo"
    layer_count: int = 4
    batch_size: int = 4
    calib_batch_size: int = 4
    default_block_size: int = 2
    state_dir: Path = Path("runtime/state")
    cache_dir: Path = Path("runtime/cache")
    temp_dir: Path = Path("runtime/tmp")
    artifacts_dir: Path = Path("artifacts")
    max_layers_per_stage: int = 64
    max_resident_weight_bytes: int = 32 * 1024 * 1024
    max_calibration_comparisons: int = 2_000_000
    max_block_elements: int = 4096
    tolerance_abs: float = 0.25
    tolerance_rel: float = 0.50
    quant_dtype_cycle: tuple[str, ...] = ("int8", "int8", "uint8", "int4")
    manifest_overrides: dict[int, dict[str, Any]] = field(default_factory=dict)
    revision_overrides: dict[int, int] = field(default_factory=dict)
    calib_seed_overrides: dict[int, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.state_dir = Path(self.state_dir)
        self.cache_dir = Path(self.cache_dir)
        self.temp_dir = Path(self.temp_dir)
        self.artifacts_dir = Path(self.artifacts_dir)
        self.max_layers_per_stage = int(self.max_layers_per_stage)
        self.max_resident_weight_bytes = int(self.max_resident_weight_bytes)
        self.max_calibration_comparisons = int(self.max_calibration_comparisons)
        self.max_block_elements = int(self.max_block_elements)
        self.manifest_overrides = {
            int(k): dict(v) for k, v in self.manifest_overrides.items()
        }
        self.revision_overrides = {
            int(k): int(v) for k, v in self.revision_overrides.items()
        }
        self.calib_seed_overrides = {
            int(k): int(v) for k, v in self.calib_seed_overrides.items()
        }
        self._validate()

    def _validate(self) -> None:
        if self.layer_count < 1:
            raise BusinessError(E_INVALID_CONFIG, "layer_count 必须大于 0")
        if self.batch_size < 1 or self.calib_batch_size < 1:
            raise BusinessError(E_INVALID_CONFIG, "batch size 必须大于 0")
        if self.default_block_size < 1:
            raise BusinessError(E_INVALID_CONFIG, "block size 必须大于 0")
        if not self.quant_dtype_cycle:
            raise BusinessError(E_INVALID_CONFIG, "quant_dtype_cycle 不能为空")
        for name in (
            "max_layers_per_stage",
            "max_resident_weight_bytes",
            "max_calibration_comparisons",
            "max_block_elements",
        ):
            if getattr(self, name) < 1:
                raise BusinessError(E_INVALID_CONFIG, f"{name} 必须大于 0")
        if self.tolerance_abs < 0 or self.tolerance_rel < 0:
            raise BusinessError(E_INVALID_CONFIG, "容差不能为负数")
        for index, revision in self.revision_overrides.items():
            if index < 0 or index >= self.layer_count:
                raise BusinessError(E_INVALID_CONFIG, f"revision override 层索引越界: {index}")
            if revision < 1:
                raise BusinessError(E_INVALID_CONFIG, f"weight_revision 必须大于 0: {index}")

    def clone(self, **changes: Any) -> "WorkbenchConfig":
        return replace(self, **changes)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key in ("state_dir", "cache_dir", "temp_dir", "artifacts_dir"):
            data[key] = str(data[key])
        data["quant_dtype_cycle"] = list(self.quant_dtype_cycle)
        return data
