"""Configuration validation and stage-scoped semantic versions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .canonical import content_hash, read_json
from .errors import E_CONFIG_INVALID, BusinessError


STAGES = ("quality", "neardup", "consistency", "sampling", "manifest")


@dataclass(frozen=True)
class ConfigSnapshot:
    payload: dict[str, Any]
    version: int
    checksum: str

    def stage_version(self, stage: str) -> str:
        if stage not in STAGES:
            raise KeyError(stage)
        relevant: dict[str, Any] = {
            "schema_version": self.payload["schema_version"],
            "stage": stage,
        }
        if stage == "quality":
            relevant["quality"] = self.payload["quality"]
            relevant["precision"] = self.payload["precision"]
        elif stage == "neardup":
            relevant["neardup"] = self.payload["neardup"]
        elif stage == "consistency":
            relevant["consistency"] = self.payload["consistency"]
        elif stage == "sampling":
            relevant["sampling"] = self.payload["sampling"]
            relevant["seed"] = self.payload["seed"]
        elif stage == "manifest":
            relevant["manifest"] = self.payload.get("manifest", {})
        return content_hash(relevant)


def load_config(path: str) -> ConfigSnapshot:
    try:
        raw = read_json(path)
    except Exception as exc:  # noqa: BLE001 - re-raised with a business code
        raise BusinessError(E_CONFIG_INVALID, f"cannot read config: {exc}") from exc
    return validate_config(raw)


def validate_config(raw: Any) -> ConfigSnapshot:
    if not isinstance(raw, dict):
        raise BusinessError(E_CONFIG_INVALID, "config must be a JSON object")
    payload = dict(raw)
    embedded_checksum = payload.pop("checksum", None)
    version = payload.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise BusinessError(E_CONFIG_INVALID, "version must be a positive integer")

    required = {
        "schema_version",
        "version",
        "seed",
        "quality",
        "neardup",
        "consistency",
        "sampling",
        "scheduler",
        "precision",
        "priority_order",
    }
    missing = sorted(required - set(payload))
    if missing:
        raise BusinessError(
            E_CONFIG_INVALID, "config is missing required sections", details={"missing": missing}
        )
    if not isinstance(payload["seed"], int) or isinstance(payload["seed"], bool):
        raise BusinessError(E_CONFIG_INVALID, "seed must be an integer")
    if payload["schema_version"] != "1.0":
        raise BusinessError(E_CONFIG_INVALID, "only schema_version 1.0 is supported")
    if payload["precision"] not in ("fp64",):
        raise BusinessError(E_CONFIG_INVALID, "precision must currently be 'fp64'")

    _validate_quality(payload["quality"])
    _validate_neardup(payload["neardup"])
    _validate_consistency(payload["consistency"])
    _validate_sampling(payload["sampling"])
    _validate_scheduler(payload["scheduler"])

    allowed_priorities = {
        "progress",
        "memory",
        "grouping_quality",
        "parallelism",
        "wall_time",
        "batch_efficiency",
    }
    priorities = payload["priority_order"]
    if (
        not isinstance(priorities, list)
        or not priorities
        or len(set(priorities)) != len(priorities)
        or any(item not in allowed_priorities for item in priorities)
    ):
        raise BusinessError(
            E_CONFIG_INVALID,
            "priority_order must be a unique non-empty subset of declared priorities",
            details={"allowed": sorted(allowed_priorities)},
        )
    for priority in ("progress", "memory"):
        if priority in priorities and priorities.index(priority) > 1:
            raise BusinessError(
                E_CONFIG_INVALID,
                "progress and memory must not be deprioritized below safety constraints",
            )

    actual_checksum = content_hash(payload)
    if embedded_checksum is not None and embedded_checksum != actual_checksum:
        raise BusinessError(
            "E_CHECKSUM_MISMATCH",
            "embedded config checksum does not match config body",
            details={"stored": embedded_checksum, "actual": actual_checksum},
        )
    return ConfigSnapshot(payload=payload, version=version, checksum=actual_checksum)


def _require_number(value: Any, label: str, *, minimum: float | None = None) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise BusinessError(E_CONFIG_INVALID, f"{label} must be numeric")
    number = float(value)
    if minimum is not None and number < minimum:
        raise BusinessError(E_CONFIG_INVALID, f"{label} must be >= {minimum}")
    return number


def _require_int(value: Any, label: str, *, minimum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise BusinessError(E_CONFIG_INVALID, f"{label} must be an integer >= {minimum}")
    return value


def _validate_quality(quality: Any) -> None:
    if not isinstance(quality, dict):
        raise BusinessError(E_CONFIG_INVALID, "quality must be an object")
    weights = quality.get("weights")
    if not isinstance(weights, dict):
        raise BusinessError(E_CONFIG_INVALID, "quality.weights must be an object")
    expected = {"length", "repetition", "noise"}
    if set(weights) != expected:
        raise BusinessError(
            E_CONFIG_INVALID,
            "quality.weights must contain exactly length, repetition, noise",
        )
    total = sum(_require_number(weights[key], f"quality.weights.{key}", minimum=0.0) for key in expected)
    if abs(total - 1.0) > 1e-9:
        raise BusinessError(E_CONFIG_INVALID, "quality weights must sum to 1.0")
    _require_number(quality.get("target_chars"), "quality.target_chars", minimum=1.0)
    _require_number(quality.get("pass_threshold"), "quality.pass_threshold", minimum=0.0)
    tiers = quality.get("tiers")
    if not isinstance(tiers, list) or not tiers:
        raise BusinessError(E_CONFIG_INVALID, "quality.tiers must be a non-empty list")
    previous_floor = 2.0
    for tier in tiers:
        if not isinstance(tier, dict) or not isinstance(tier.get("name"), str):
            raise BusinessError(E_CONFIG_INVALID, "each quality tier needs a string name")
        floor = _require_number(tier.get("min_score"), "quality tier min_score")
        if floor > previous_floor:
            raise BusinessError(E_CONFIG_INVALID, "quality tiers must be ordered by descending min_score")
        previous_floor = floor


def _validate_neardup(neardup: Any) -> None:
    if not isinstance(neardup, dict):
        raise BusinessError(E_CONFIG_INVALID, "neardup must be an object")
    _require_int(neardup.get("chunk_tokens"), "neardup.chunk_tokens", minimum=1)
    threshold = _require_number(neardup.get("threshold"), "neardup.threshold", minimum=0.0)
    if threshold > 1.0:
        raise BusinessError(E_CONFIG_INVALID, "neardup.threshold must be <= 1.0")
    if neardup.get("threshold_mode") not in ("strict", "tolerance"):
        raise BusinessError(E_CONFIG_INVALID, "neardup.threshold_mode must be strict or tolerance")
    tolerance = _require_number(neardup.get("tolerance", 0.0), "neardup.tolerance", minimum=0.0)
    if tolerance > 1.0:
        raise BusinessError(E_CONFIG_INVALID, "neardup.tolerance must be <= 1.0")


def _validate_consistency(consistency: Any) -> None:
    if not isinstance(consistency, dict):
        raise BusinessError(E_CONFIG_INVALID, "consistency must be an object")
    _require_number(consistency.get("smoothing"), "consistency.smoothing", minimum=0.0)
    if consistency.get("normalization", "nfkc_casefold_ws") != "nfkc_casefold_ws":
        raise BusinessError(E_CONFIG_INVALID, "unsupported consistency.normalization")


def _validate_sampling(sampling: Any) -> None:
    if not isinstance(sampling, dict):
        raise BusinessError(E_CONFIG_INVALID, "sampling must be an object")
    quotas = sampling.get("quotas")
    if not isinstance(quotas, list):
        raise BusinessError(E_CONFIG_INVALID, "sampling.quotas must be a list")
    seen: set[tuple[str, str]] = set()
    for index, quota in enumerate(quotas):
        if not isinstance(quota, dict):
            raise BusinessError(E_CONFIG_INVALID, f"sampling.quotas[{index}] must be an object")
        group = quota.get("annotator_group")
        tier = quota.get("quality_tier")
        if not isinstance(group, str) or not group or not isinstance(tier, str) or not tier:
            raise BusinessError(E_CONFIG_INVALID, "quota group and tier must be non-empty strings")
        _require_int(quota.get("target"), f"sampling.quotas[{index}].target", minimum=0)
        key = (group, tier)
        if key in seen:
            raise BusinessError(E_CONFIG_INVALID, "duplicate sampling quota", details={"quota": key})
        seen.add(key)
    max_total = sampling.get("max_total")
    if max_total is not None:
        _require_int(max_total, "sampling.max_total", minimum=0)


def _validate_scheduler(scheduler: Any) -> None:
    if not isinstance(scheduler, dict):
        raise BusinessError(E_CONFIG_INVALID, "scheduler must be an object")
    _require_int(scheduler.get("max_workers"), "scheduler.max_workers", minimum=1)
    _require_int(scheduler.get("max_batch_pairs"), "scheduler.max_batch_pairs", minimum=1)
    _require_int(scheduler.get("mac_budget"), "scheduler.mac_budget", minimum=1)
    _require_int(scheduler.get("memory_budget_bytes"), "scheduler.memory_budget_bytes", minimum=1)
    model = scheduler.get("cost_model")
    if not isinstance(model, dict):
        raise BusinessError(E_CONFIG_INVALID, "scheduler.cost_model must be an object")
    for key in (
        "mac_per_pair",
        "mac_per_comparison",
        "mac_reduction_per_batch",
        "mac_worker_overhead",
        "memory_fixed_bytes",
        "memory_per_worker_bytes",
        "memory_per_pair_bytes",
        "memory_per_comparison_bytes",
    ):
        _require_int(model.get(key), f"scheduler.cost_model.{key}", minimum=0)
