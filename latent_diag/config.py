from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import EngineError
from .hashing import hash_json


DEFAULT_VOCABULARY = [
    "<bos>",
    "<eos>",
    "<unk>",
    "a",
    "an",
    "the",
    "and",
    "with",
    "in",
    "on",
    "of",
    "red",
    "blue",
    "green",
    "orange",
    "silver",
    "gold",
    "white",
    "black",
    "forest",
    "city",
    "ocean",
    "desert",
    "snow",
    "mountain",
    "river",
    "street",
    "garden",
    "room",
    "fox",
    "robot",
    "bird",
    "flower",
    "tree",
    "boat",
    "bridge",
    "tower",
    "sun",
    "moon",
    "cloud",
    "light",
    "shadow",
    "water",
    "glass",
    "metal",
    "fabric",
    "stone",
    "paper",
    "morning",
    "evening",
    "night",
    "quiet",
    "bright",
    "soft",
    "sharp",
    "simple",
    "detailed",
    "cinematic",
    "minimal",
    "abstract",
    "portrait",
    "landscape",
    "still",
    "life",
    "style",
    "scene",
    "photo",
    "painted",
]


DEFAULT_CONFIG: dict[str, Any] = {
    "schema_version": 1,
    "condition_dim": 64,
    "vocabulary": DEFAULT_VOCABULARY,
    "phase": {"amplitude": 0.0025},
    "denoise": {"base_decay": 0.95, "boundary_mix": 0.08, "bias_scale": 0.01},
    "decode": {"contrast": 0.55},
    "budget": {
        "max_total_denoise_iterations": 10000,
        "max_resident_latent_bytes": 64 * 1024 * 1024,
        "worker_count": 2,
        "tasks_per_batch": 2,
        "priority": ["iterations", "memory"],
    },
    "tolerance": {
        "max_abs": 0.02,
        "max_rel": 0.02,
        "max_amplification": 1000.0,
    },
}


def _merge_defaults(value: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(defaults)
    for key, item in value.items():
        if isinstance(item, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_defaults(item, merged[key])
        else:
            merged[key] = copy.deepcopy(item)
    return merged


def _normalize_aliases(raw: dict[str, Any]) -> dict[str, Any]:
    normalized = copy.deepcopy(raw)
    budget = normalized.get("budget")
    if isinstance(budget, dict):
        aliases = {
            "max_total_denoise_iterations": (
                "max_iterations",
                "iteration_budget",
                "max_total_iterations",
            ),
            "max_resident_latent_bytes": (
                "max_memory_bytes",
                "memory_budget",
                "max_peak_memory_bytes",
            ),
            "worker_count": ("workers", "max_workers"),
            "tasks_per_batch": ("batch_size", "max_tasks_per_batch"),
        }
        for canonical, alternatives in aliases.items():
            if canonical not in budget:
                for alternative in alternatives:
                    if alternative in budget:
                        budget[canonical] = budget[alternative]
                        break
    tolerance = normalized.get("tolerance")
    if isinstance(tolerance, dict):
        if "max_abs" not in tolerance:
            for alternative in ("abs_tolerance", "atol"):
                if alternative in tolerance:
                    tolerance["max_abs"] = tolerance[alternative]
                    break
        if "max_rel" not in tolerance:
            for alternative in ("rel_tolerance", "rtol"):
                if alternative in tolerance:
                    tolerance["max_rel"] = tolerance[alternative]
                    break
        if "max_amplification" not in tolerance:
            for alternative in ("amplification_limit", "max_error_amplification"):
                if alternative in tolerance:
                    tolerance["max_amplification"] = tolerance[alternative]
                    break
    return normalized


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool):
        valid = False
    elif isinstance(value, int):
        valid = value > 0
    elif isinstance(value, float):
        valid = value.is_integer() and value > 0
    else:
        valid = False
    if not valid:
        raise EngineError(
            "E_BUDGET_INVALID",
            f"{name} must be a positive integer",
            details={name: value},
        )
    return int(value)


def _positive_number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise EngineError(
            "E_BUDGET_INVALID",
            f"{name} must be positive",
            details={name: value},
        )
    return float(value)


@dataclass
class EffectiveConfig:
    raw: dict[str, Any]
    path: str | None = None

    @property
    def condition_dim(self) -> int:
        return int(self.raw["condition_dim"])

    @property
    def vocabulary(self) -> list[str]:
        return [str(v) for v in self.raw["vocabulary"]]

    @property
    def budget(self) -> dict[str, Any]:
        return self.raw["budget"]

    @property
    def tolerance(self) -> dict[str, Any]:
        return self.raw["tolerance"]

    @property
    def phase(self) -> dict[str, Any]:
        return self.raw["phase"]

    @property
    def denoise(self) -> dict[str, Any]:
        return self.raw["denoise"]

    @property
    def decode(self) -> dict[str, Any]:
        return self.raw["decode"]

    def computational_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.raw["schema_version"],
            "condition_dim": self.raw["condition_dim"],
            "vocabulary": self.raw["vocabulary"],
            "phase": self.raw["phase"],
            "denoise": self.raw["denoise"],
            "decode": self.raw["decode"],
        }

    @property
    def compute_hash(self) -> str:
        return hash_json(self.computational_payload())

    @property
    def budget_hash(self) -> str:
        return hash_json(self.budget)

    def with_overrides(
        self,
        *,
        worker_count: int | None = None,
        tasks_per_batch: int | None = None,
    ) -> "EffectiveConfig":
        raw = copy.deepcopy(self.raw)
        if worker_count is not None:
            raw["budget"]["worker_count"] = worker_count
        if tasks_per_batch is not None:
            raw["budget"]["tasks_per_batch"] = tasks_per_batch
        return validate_config(raw, path=self.path)


def validate_config(raw: dict[str, Any], path: str | None = None) -> EffectiveConfig:
    if not isinstance(raw, dict):
        raise EngineError("E_CONFIG_INVALID", "config root must be a JSON object")
    merged = _merge_defaults(_normalize_aliases(raw), DEFAULT_CONFIG)
    condition_dim = merged.get("condition_dim")
    if isinstance(condition_dim, bool) or not isinstance(condition_dim, int) or condition_dim <= 0:
        raise EngineError(
            "E_CONFIG_INVALID",
            "condition_dim must be a positive integer",
            details={"condition_dim": condition_dim},
        )
    vocabulary = merged.get("vocabulary")
    if not isinstance(vocabulary, list) or not vocabulary:
        raise EngineError("E_CONFIG_INVALID", "vocabulary must be a non-empty list")
    if "<unk>" not in vocabulary:
        raise EngineError("E_CONFIG_INVALID", "vocabulary must contain <unk>")

    budget = merged["budget"]
    _positive_int(budget.get("max_total_denoise_iterations"), "max_total_denoise_iterations")
    _positive_int(budget.get("max_resident_latent_bytes"), "max_resident_latent_bytes")
    _positive_int(budget.get("worker_count"), "worker_count")
    _positive_int(budget.get("tasks_per_batch"), "tasks_per_batch")
    priority = budget.get("priority")
    if (
        not isinstance(priority, list)
        or not priority
        or set(priority) != {"iterations", "memory"}
        or len(priority) != 2
    ):
        raise EngineError(
            "E_BUDGET_INVALID",
            "budget.priority must list iterations and memory exactly once",
            details={"priority": priority},
        )

    tolerance = merged["tolerance"]
    for key in ("max_abs", "max_rel", "max_amplification"):
        _positive_number(tolerance.get(key), f"tolerance.{key}")

    boundary_mix = merged["denoise"].get("boundary_mix")
    if not isinstance(boundary_mix, (int, float)) or not 0 <= float(boundary_mix) <= 1:
        raise EngineError(
            "E_CONFIG_INVALID",
            "denoise.boundary_mix must be in [0, 1]",
            details={"boundary_mix": boundary_mix},
        )
    _positive_number(merged["phase"].get("amplitude"), "phase.amplitude")
    _positive_number(merged["decode"].get("contrast"), "decode.contrast")
    return EffectiveConfig(raw=merged, path=path)


def load_config(path: str | Path) -> EffectiveConfig:
    import json

    config_path = Path(path)
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise EngineError("E_CONFIG_NOT_FOUND", f"config not found: {config_path}") from exc
    except json.JSONDecodeError as exc:
        raise EngineError(
            "E_CONFIG_INVALID",
            f"config is not valid JSON: {exc}",
            details={"path": str(config_path)},
        ) from exc
    return validate_config(raw, path=str(config_path))

