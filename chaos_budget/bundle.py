from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from .contracts import (
    bundle_fingerprint,
    input_file_hash,
    read_json_file,
    validate_budget_config,
    validate_experiments,
    validate_observations,
)
from .errors import ChaosError
from .utils import canonical_json, round_decimal_to_ms, sha256_text


FORMAT_PRIORITY = {
    "v1_json": 10,
    "v2_shard_sqlite": 20,
}


def _first_existing(directory: Path, names: List[str]) -> Optional[Path]:
    for name in names:
        path = directory / name
        if path.exists():
            return path
    return None


def _policy_entry(
    key: str,
    entry: Any,
    snapshot_clock: int,
    snapshot_generation: int,
) -> Dict[str, Any]:
    if isinstance(entry, Mapping) and "value" in entry:
        declared_clock = entry.get("declared_logical_clock_ms", snapshot_clock)
        generation = entry.get("generation", snapshot_generation)
        value = entry["value"]
    else:
        declared_clock = snapshot_clock
        generation = snapshot_generation
        value = entry

    if not isinstance(declared_clock, int) or isinstance(declared_clock, bool) or declared_clock < 0:
        raise ChaosError(
            "INVALID_POLICY_CLOCK",
            f"policy {key!r} has invalid declared logical clock",
            details={"policy_key": key},
        )
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
        raise ChaosError(
            "INVALID_POLICY_GENERATION",
            f"policy {key!r} has invalid generation",
            details={"policy_key": key},
        )
    return {
        "key": key,
        "value": value,
        "declared_clock_ms": declared_clock,
        "generation": generation,
    }


def _load_v1_policy(path: Path, injection_clock_ms: int) -> List[Dict[str, Any]]:
    payload = read_json_file(path)
    if not isinstance(payload, Mapping):
        raise ChaosError("SCHEMA_VIOLATION", "v1 policy snapshot must be a JSON object", {"path": str(path)})

    snapshot_clock = payload.get("declared_logical_clock_ms", 0)
    generation = payload.get("generation", 1)
    if not isinstance(snapshot_clock, int) or isinstance(snapshot_clock, bool) or snapshot_clock < 0:
        raise ChaosError("INVALID_POLICY_CLOCK", "v1 snapshot declared_logical_clock_ms must be non-negative integer")
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
        raise ChaosError("INVALID_POLICY_GENERATION", "v1 snapshot generation must be a positive integer")

    conversion = None
    relative_seconds = payload.get("effective_relative_seconds")
    if relative_seconds is not None:
        rounding = payload.get("rounding", "ceil")
        try:
            conversion = round_decimal_to_ms(relative_seconds, rounding)
        except Exception as exc:  # decimal/invalid rounding
            raise ChaosError(
                "INVALID_POLICY_TIME_CONVERSION",
                "v1 effective_relative_seconds cannot be converted",
                {"value": relative_seconds, "rounding": rounding},
            ) from exc
        conversion["base_injection_clock_ms"] = injection_clock_ms
        conversion["effective_absolute_clock_ms"] = (
            injection_clock_ms + conversion["rounded_milliseconds"]
        )

    policies = payload.get("policies", {})
    if not isinstance(policies, Mapping):
        raise ChaosError("SCHEMA_VIOLATION", "v1 policies must be an object")

    candidates: List[Dict[str, Any]] = []
    for key, entry in policies.items():
        candidate = _policy_entry(key, entry, snapshot_clock, generation)
        candidate.update(
            {
                "source_format": "v1_json",
                "source_priority": FORMAT_PRIORITY["v1_json"],
                "source_file": str(path),
                "time_conversion": conversion,
            }
        )
        candidates.append(candidate)
    return candidates


def _connect_policy_metadata(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise ChaosError("INPUT_NOT_FOUND", f"v2 policy metadata not found: {path}", {"path": str(path)})
    uri = f"{path.resolve().as_uri()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("SELECT 1 FROM snapshot LIMIT 1")
        conn.execute("SELECT 1 FROM shards LIMIT 1")
        conn.execute("SELECT 1 FROM policy_meta LIMIT 1")
        return conn
    except sqlite3.Error as exc:
        raise ChaosError(
            "POLICY_METADATA_INVALID",
            f"cannot read v2 SQLite policy metadata: {exc}",
            {"path": str(path)},
        ) from exc


def _load_v2_policy(metadata_path: Path, directory: Path) -> List[Dict[str, Any]]:
    conn = _connect_policy_metadata(metadata_path)
    try:
        snapshot = conn.execute("SELECT * FROM snapshot LIMIT 1").fetchone()
        if snapshot is None:
            raise ChaosError("POLICY_METADATA_INVALID", "v2 snapshot table is empty")
        snapshot_clock = int(snapshot["declared_logical_clock_ms"])
        snapshot_generation = int(snapshot["generation"])

        shard_rows = conn.execute("SELECT * FROM shards ORDER BY shard_file").fetchall()
        meta_rows = conn.execute("SELECT * FROM policy_meta ORDER BY policy_key").fetchall()
        shard_contents: Dict[str, Mapping[str, Any]] = {}

        for shard_row in shard_rows:
            shard_name = shard_row["shard_file"]
            shard_path = directory / shard_name
            if not shard_path.exists():
                raise ChaosError("INPUT_NOT_FOUND", f"policy shard missing: {shard_name}")
            actual_hash = input_file_hash(shard_path)
            if actual_hash != shard_row["sha256"]:
                raise ChaosError(
                    "POLICY_SHARD_CHECKSUM_MISMATCH",
                    f"policy shard checksum mismatch: {shard_name}",
                    {"expected": shard_row["sha256"], "actual": actual_hash},
                )
            payload = read_json_file(shard_path)
            if not isinstance(payload, Mapping) or not isinstance(payload.get("policies"), Mapping):
                raise ChaosError("SCHEMA_VIOLATION", f"policy shard {shard_name} must contain policies object")
            shard_contents[shard_name] = payload["policies"]

        candidates: List[Dict[str, Any]] = []
        for meta in meta_rows:
            key = meta["policy_key"]
            shard_name = meta["shard_file"]
            policies = shard_contents.get(shard_name)
            if policies is None or key not in policies:
                raise ChaosError(
                    "POLICY_METADATA_INVALID",
                    f"policy key {key!r} metadata does not match shard {shard_name}",
                )
            candidate = _policy_entry(
                key,
                policies[key],
                int(meta["declared_logical_clock_ms"]),
                int(meta["generation"] or snapshot_generation),
            )
            if candidate["declared_clock_ms"] != int(meta["declared_logical_clock_ms"]):
                raise ChaosError("POLICY_METADATA_INVALID", f"policy clock mismatch for {key}")
            candidate.update(
                {
                    "source_format": "v2_shard_sqlite",
                    "source_priority": FORMAT_PRIORITY["v2_shard_sqlite"],
                    "source_file": str(directory / shard_name),
                    "metadata_file": str(metadata_path),
                    "time_conversion": None,
                }
            )
            candidates.append(candidate)
        return candidates
    finally:
        conn.close()


def merge_policy_candidates(candidates: List[Dict[str, Any]]) -> Dict[str, Any]:
    by_key: Dict[str, List[Dict[str, Any]]] = {}
    for candidate in candidates:
        by_key.setdefault(candidate["key"], []).append(candidate)

    selected: Dict[str, Dict[str, Any]] = {}
    for key, versions in sorted(by_key.items()):
        if len(versions) == 1:
            winner = versions[0]
            reason = "only_available_version"
        else:
            winner = max(
                versions,
                key=lambda item: (
                    item["declared_clock_ms"],
                    item["generation"],
                    item["source_priority"],
                ),
            )
            sorted_versions = sorted(
                versions,
                key=lambda item: (
                    item["declared_clock_ms"],
                    item["generation"],
                    item["source_priority"],
                ),
                reverse=True,
            )
            runner_up = sorted_versions[1]
            if winner["declared_clock_ms"] != runner_up["declared_clock_ms"]:
                reason = "higher_declared_logical_clock"
            elif winner["generation"] != runner_up["generation"]:
                reason = "higher_policy_generation"
            else:
                reason = "equal_clock_and_generation_used_source_format_priority"

        selected[key] = {
            "key": key,
            "value": winner["value"],
            "source_format": winner["source_format"],
            "source_file": winner["source_file"],
            "declared_clock_ms": winner["declared_clock_ms"],
            "generation": winner["generation"],
            "format_priority": winner["source_priority"],
            "selection_reason": reason,
            "time_conversion": winner.get("time_conversion"),
            "candidate_count": len(versions),
            "arbitration_hash": sha256_text(
                canonical_json(
                    {
                        "winner": {
                            k: winner[k]
                            for k in (
                                "key",
                                "value",
                                "declared_clock_ms",
                                "generation",
                                "source_format",
                            )
                        },
                        "reason": reason,
                    }
                )
            ),
        }

    return {
        "policies": selected,
        "values": {key: item["value"] for key, item in selected.items()},
        "format_priority": FORMAT_PRIORITY,
    }


def load_bundle(sample_dir: Path | str, *, injection_clock_ms: Optional[int] = None) -> Dict[str, Any]:
    directory = Path(sample_dir)
    if not directory.exists():
        raise ChaosError("SAMPLE_DIR_NOT_FOUND", f"sample directory not found: {directory}", {"path": str(directory)})

    experiments_path = _first_existing(directory, ["experiments.json", "experiment_plan.json", "plans.json"])
    budget_path = _first_existing(directory, ["budget_config.json", "budget.json"])
    observations_path = _first_existing(directory, ["observations.json", "observation_events.json", "events.json"])
    if experiments_path is None:
        raise ChaosError("INPUT_NOT_FOUND", "experiments.json is required", {"sample_dir": str(directory)})
    if budget_path is None:
        raise ChaosError("INPUT_NOT_FOUND", "budget_config.json is required", {"sample_dir": str(directory)})
    if observations_path is None:
        raise ChaosError("INPUT_NOT_FOUND", "observations.json is required", {"sample_dir": str(directory)})

    experiments_payload = read_json_file(experiments_path)
    budget_payload = read_json_file(budget_path)
    observations_payload = read_json_file(observations_path)

    experiments = validate_experiments(experiments_payload)
    budget = validate_budget_config(budget_payload)
    observations = validate_observations(
        observations_payload,
        known_experiment_ids=[item["experiment_id"] for item in experiments],
    )

    manifest_path = directory / "manifest.json"
    manifest: Dict[str, Any] = {}
    if manifest_path.exists():
        manifest_payload = read_json_file(manifest_path)
        if isinstance(manifest_payload, Mapping):
            manifest = dict(manifest_payload)

    if injection_clock_ms is None:
        injection_clock_ms = manifest.get("injection_clock_ms")
    if injection_clock_ms is None:
        injection_clock_ms = min(item["start_clock_ms"] for item in experiments)
    if not isinstance(injection_clock_ms, int) or isinstance(injection_clock_ms, bool) or injection_clock_ms < 0:
        raise ChaosError("INVALID_CLOCK", "injection clock must be a non-negative integer")

    v1_path = _first_existing(
        directory,
        ["policy_v1.json", "policy_snapshot_v1.json", "budget_policy_v1.json"],
    )
    v2_metadata_path = _first_existing(
        directory,
        ["policy_v2_metadata.sqlite3", "policy_v2_metadata.db", "policy_metadata.sqlite3"],
    )
    if v1_path is None:
        raise ChaosError("INPUT_NOT_FOUND", "v1 policy JSON snapshot is required")
    if v2_metadata_path is None:
        raise ChaosError("INPUT_NOT_FOUND", "v2 shard JSON plus SQLite metadata snapshot is required")

    candidates = _load_v1_policy(v1_path, injection_clock_ms)
    candidates.extend(_load_v2_policy(v2_metadata_path, directory))
    policy_view = merge_policy_candidates(candidates)

    hash_paths = [experiments_path, budget_path, observations_path, v1_path, v2_metadata_path]
    hash_paths.extend(sorted(directory.glob("policy_v2_shard_*.json")))
    file_hashes = {str(path.relative_to(directory)): input_file_hash(path) for path in hash_paths if path.exists()}

    return {
        "sample_dir": str(directory),
        "manifest": manifest,
        "injection_clock_ms": injection_clock_ms,
        "experiments": experiments,
        "budget_config": budget,
        "observations": observations,
        "policy_view": policy_view,
        "file_hashes": file_hashes,
        "fingerprint": bundle_fingerprint(file_hashes),
        "paths": {
            "experiments": str(experiments_path),
            "budget_config": str(budget_path),
            "observations": str(observations_path),
            "policy_v1": str(v1_path),
            "policy_v2_metadata": str(v2_metadata_path),
        },
    }


def policy_int(policy_view: Mapping[str, Any], key: str, default: int) -> int:
    values = policy_view.get("values", {}) if "values" in policy_view else policy_view
    value = values.get(key, default)
    try:
        if isinstance(value, bool):
            return int(value)
        return int(value)
    except (TypeError, ValueError):
        return default
