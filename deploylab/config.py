from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class BudgetConfig:
    max_layers_per_stage: int = 64
    max_resident_bytes: int = 32 * 1024 * 1024
    max_quant_macs: int = 200_000_000
    max_inference_macs: int = 200_000_000
    allow_degrade: bool = True
    degraded_group_size: int = 8
    inference_tile_min_rows: int = 1

    def validate(self) -> None:
        for name in (
            "max_layers_per_stage",
            "max_resident_bytes",
            "max_quant_macs",
            "max_inference_macs",
            "degraded_group_size",
            "inference_tile_min_rows",
        ):
            if int(getattr(self, name)) <= 0:
                raise ValueError(f"budget.{name} must be > 0")


@dataclass
class LabConfig:
    calib_samples: int = 8
    budget: BudgetConfig = field(default_factory=BudgetConfig)
    sqlite_busy_timeout_ms: int = 0
    error_busy_timeout_ms: int = 1500
    default_bundle: str = "data/sample"
    default_db: str = "artifacts/deploylab.sqlite"
    crash_after_layers: int = 2

    def validate(self) -> None:
        if self.calib_samples <= 0:
            raise ValueError("calib_samples must be > 0")
        self.budget.validate()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LabConfig":
        budget_data = dict(data.get("budget") or {})
        known = {
            "calib_samples",
            "sqlite_busy_timeout_ms",
            "error_busy_timeout_ms",
            "default_bundle",
            "default_db",
            "crash_after_layers",
        }
        kwargs = {key: data[key] for key in known if key in data}
        cfg = cls(budget=BudgetConfig(**budget_data), **kwargs)
        cfg.validate()
        return cfg

    @classmethod
    def load(cls, path: str | Path | None) -> "LabConfig":
        if path is None:
            cfg = cls()
            cfg.validate()
            return cfg
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    def save(self, path: str | Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )

