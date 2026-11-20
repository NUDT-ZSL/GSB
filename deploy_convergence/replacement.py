"""Package replacement classification and rollback policy."""

from __future__ import annotations

from typing import Any

from .errors import EngineError


def _semver(value: str) -> tuple[int, int, int]:
    try:
        parts = tuple(int(part) for part in value.split("."))
    except (AttributeError, ValueError) as exc:
        raise EngineError(
            "E_BAD_MANIFEST",
            "manifest version is not semantic",
            stage="replace",
            details={"manifest_version": value},
        ) from exc
    if len(parts) != 3:
        raise EngineError(
            "E_BAD_MANIFEST",
            "manifest version must have three numeric components",
            stage="replace",
            details={"manifest_version": value},
        )
    return parts


def classify_change(
    current: dict[str, Any] | None,
    incoming: dict[str, Any],
    *,
    clock_ms: int,
    rollback_policy: str,
    rollback_window_ms: int,
) -> dict[str, Any]:
    if rollback_policy not in {"reject", "degrade"}:
        raise EngineError(
            "E_BAD_MANIFEST",
            "rollback_policy must be reject or degrade",
            stage="replace",
            details={"rollback_policy": rollback_policy},
        )
    if current is None:
        return {
            "change_kind": "initial",
            "reason": "no_committed_view",
            "basis": ["current_view_absent"],
        }
    comparisons = {
        "manifest_version": incoming["manifest_version"]
        == current["manifest_version"],
        "generation_min": incoming["generation_min"]
        == current["generation_min"],
        "shard_hashes": incoming["shard_hashes"] == current["shard_hashes"],
        "parameter_digest": incoming["parameter_digest"]
        == current["parameter_digest"],
        "input_digest": incoming["input_digest"] == current["input_digest"],
    }
    required_comparisons = (
        "manifest_version",
        "generation_min",
        "shard_hashes",
        "parameter_digest",
    )
    if all(comparisons[key] for key in required_comparisons):
        return {
            "change_kind": "noop",
            "reason": "all_change_inputs_equal",
            "basis": list(required_comparisons),
            "input_digest_equal": comparisons["input_digest"],
        }

    current_version = _semver(current["manifest_version"])
    incoming_version = _semver(incoming["manifest_version"])
    if incoming_version < current_version:
        age_ms = clock_ms - int(current.get("committed_at_ms", clock_ms))
        within_window = 0 <= age_ms <= rollback_window_ms
        if rollback_policy == "reject":
            raise EngineError(
                "E_VERSION_ROLLBACK",
                "manifest version rollback is rejected by policy",
                stage="replace",
                details={
                    "policy": rollback_policy,
                    "current_manifest_version": current["manifest_version"],
                    "incoming_manifest_version": incoming["manifest_version"],
                    "clock_ms": clock_ms,
                    "current_committed_at_ms": current.get("committed_at_ms"),
                    "rollback_window_ms": rollback_window_ms,
                    "within_rollback_window": within_window,
                    "decision": "reject",
                },
            )
        if not within_window:
            raise EngineError(
                "E_VERSION_ROLLBACK",
                "rollback is outside the injected-clock rollback window",
                stage="replace",
                details={
                    "policy": rollback_policy,
                    "current_manifest_version": current["manifest_version"],
                    "incoming_manifest_version": incoming["manifest_version"],
                    "clock_ms": clock_ms,
                    "current_committed_at_ms": current.get("committed_at_ms"),
                    "rollback_window_ms": rollback_window_ms,
                    "within_rollback_window": within_window,
                    "decision": "degrade",
                },
            )
        return {
            "change_kind": "rollback_degraded",
            "reason": "rollback_allowed_within_injected_clock_window",
            "policy": rollback_policy,
            "basis": [
                "lower_manifest_version",
                "rollback_window_accepted",
                "degraded_commit",
            ],
            "clock_ms": clock_ms,
            "current_committed_at_ms": current.get("committed_at_ms"),
            "rollback_window_ms": rollback_window_ms,
        }

    if incoming["generation_min"] < current["generation_min"]:
        raise EngineError(
            "E_GENERATION_CONFLICT",
            "incoming generation_min is lower than the committed generation",
            stage="replace",
            details={
                "current_generation_min": current["generation_min"],
                "incoming_generation_min": incoming["generation_min"],
                "current_manifest_version": current["manifest_version"],
                "incoming_manifest_version": incoming["manifest_version"],
                "decision": "reject_generation_regression",
            },
        )

    current_shards = current.get("shard_hashes", {})
    incoming_shards = incoming.get("shard_hashes", {})
    is_shard_superset = all(
        incoming_shards.get(shard_id) == digest
        for shard_id, digest in current_shards.items()
    )
    if (
        incoming["manifest_version"] == current["manifest_version"]
        and incoming["generation_min"] >= current["generation_min"]
        and is_shard_superset
    ):
        return {
            "change_kind": "incremental",
            "reason": "same_version_shard_superset_with_new_digest",
            "basis": [
                "same_manifest_version",
                "nondecreasing_generation_min",
                "current_shards_preserved",
            ],
        }
    return {
        "change_kind": "full_replace",
        "reason": "version_or_shard_set_changed",
        "basis": [
            f"manifest_version_equal={comparisons['manifest_version']}",
            f"generation_min_equal={comparisons['generation_min']}",
            f"shard_hashes_equal={comparisons['shard_hashes']}",
            f"parameter_digest_equal={comparisons['parameter_digest']}",
        ],
    }
