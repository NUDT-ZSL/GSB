from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import TrainingConfig
from .io import atomic_write_json, sha256_json


def build_consistency_report(
    *,
    config: TrainingConfig,
    manifest: dict[str, Any],
    rounds: list[dict[str, Any]],
    final_params_digest: str,
    checkpoint_verifies_at_its_round: bool,
    checkpoint_round: int,
    round_cursor: int,
    snapshot: dict[str, Any] | None,
    shard_files_verified: bool,
    split_lossless: bool,
) -> dict[str, Any]:
    generations = manifest.get("generations", [])
    total_bytes = sum(int(item.get("payload_bytes", 0)) for item in generations)
    checks = {
        "budget_within_limit": total_bytes <= config.global_budget_bytes,
        "retention_within_configured_limit": len(generations) <= config.retention_generations,
        "manifest_total_matches_generations": total_bytes == int(manifest.get("total_bytes", -1)),
        "current_generation_is_latest": (
            (not generations and manifest.get("current_generation") is None)
            or (
                bool(generations)
                and manifest.get("current_generation") == generations[-1]["generation_id"]
            )
        ),
        "shard_files_verified": shard_files_verified,
        "merge_split_lossless": split_lossless,
        "checkpoint_verifies_at_its_round": checkpoint_verifies_at_its_round,
        "checkpoint_round_not_ahead_of_cursor": checkpoint_round <= round_cursor,
        "snapshot_hash_matches": snapshot is not None
        and snapshot.get("config_hash") == config.config_hash,
        "round_cursor_matches": snapshot is not None
        and int(snapshot.get("round_cursor", -1)) == max(
            [int(item["round"]) for item in rounds] or [0]
        ),
    }
    return {
        "checks": checks,
        "all_passed": all(checks.values()),
        "total_checkpoint_bytes": total_bytes,
        "global_budget_bytes": config.global_budget_bytes,
        "retained_generations": len(generations),
        "max_retention_generations": config.retention_generations,
        "final_params_digest": final_params_digest,
        "checkpoint_round": checkpoint_round,
        "round_cursor": round_cursor,
    }


def seal_report(report: dict[str, Any]) -> dict[str, Any]:
    sealed = dict(report)
    sealed.pop("report_digest", None)
    sealed["report_digest"] = sha256_json(sealed)
    return sealed


def write_report(path: Path, report: dict[str, Any]) -> None:
    atomic_write_json(path, seal_report(report))


def deterministic_report_view(report: dict[str, Any]) -> dict[str, Any]:
    """Fields that must be identical for an uninterrupted and a recovered run."""
    return {
        "schema_version": report.get("schema_version"),
        "config_hash": report.get("config_hash"),
        "policy_hash": report.get("policy_hash"),
        "data_hash": report.get("data_hash"),
        "total_rounds": report.get("total_rounds"),
        "rounds": report.get("rounds"),
        "final_params_digest": report.get("final_params_digest"),
        "final_parameter_bytes": report.get("final_parameter_bytes"),
        "final_shards": report.get("final_shards"),
        "generations": report.get("generations"),
        "failures": report.get("failures"),
        "injection_hits": report.get("injection_hits"),
    }
