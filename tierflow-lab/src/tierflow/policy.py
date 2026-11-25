from __future__ import annotations

import sqlite3
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Any

from .canonical import load_json, sha256_file, sha256_json
from .errors import PolicyIntegrityError, ValidationError

SOURCE_PRIORITY = {"v1": 1, "v2": 2}


def _relative_time(raw_seconds: Any, now_clock_ms: int) -> dict[str, Any]:
    seconds = Decimal(str(raw_seconds if raw_seconds is not None else "0"))
    delta_raw_ms = seconds * Decimal(1000)
    rounded_delta_ms = int(
        delta_raw_ms.to_integral_value(rounding=ROUND_FLOOR)
    )
    remainder = delta_raw_ms - Decimal(rounded_delta_ms)
    direction = "exact" if remainder == 0 else "floor"
    return {
        "kind": "relative_seconds_injected_clock",
        "original_seconds": str(seconds),
        "base_clock_ms": now_clock_ms,
        "raw_delta_ms": str(delta_raw_ms),
        "rounded_delta_ms": rounded_delta_ms,
        "absolute_effective_ms": now_clock_ms + rounded_delta_ms,
        "rounding_direction": direction,
        "rounding_remainder_ms": str(remainder),
        "rounding_margin_ms": str(Decimal(1) - remainder if remainder else Decimal(0)),
    }


def load_v1_policy(path: str | Path, now_clock_ms: int) -> dict[str, Any]:
    path = Path(path)
    data = load_json(path, decimal=True)
    if not isinstance(data, dict) or data.get("format") != "v1":
        raise ValidationError("v1 policy must be an object with format=v1")
    policies = data.get("policies")
    if not isinstance(policies, dict):
        raise ValidationError("v1 policy must contain a flat policies mapping")

    records: list[dict[str, Any]] = []
    for key in sorted(policies):
        envelope = policies[key]
        if isinstance(envelope, dict) and "value" in envelope:
            value = envelope["value"]
            declared_clock = int(
                envelope.get("declared_clock_ms", data.get("declared_clock_ms", now_clock_ms))
            )
            generation = int(envelope.get("generation", data.get("generation", 1)))
            relative_seconds = envelope.get(
                "effective_after_seconds", data.get("effective_after_seconds", 0)
            )
        else:
            value = envelope
            declared_clock = int(data.get("declared_clock_ms", now_clock_ms))
            generation = int(data.get("generation", 1))
            relative_seconds = data.get("effective_after_seconds", 0)
        time_info = _relative_time(relative_seconds, now_clock_ms)
        records.append(
            {
                "policy_key": key,
                "value": value,
                "tombstone": False,
                "source_format": "v1",
                "source_priority": SOURCE_PRIORITY["v1"],
                "source_file": str(path),
                "source_package_version": data.get("package_version", "v1"),
                "declared_clock_ms": declared_clock,
                "generation": generation,
                "absolute_effective_ms": time_info["absolute_effective_ms"],
                "original_time_representation": {
                    "kind": "relative_effective_seconds",
                    "value": str(relative_seconds),
                },
                "time_conversion": time_info,
            }
        )
    return {
        "format": "v1",
        "path": str(path),
        "file_sha256": sha256_file(path),
        "package_version": data.get("package_version", "v1"),
        "manifest_version": int(data.get("manifest_version", 1)),
        "generation": int(data.get("generation", 1)),
        "records": records,
    }


def load_v2_policy(shard_dir: str | Path, meta_db: str | Path) -> dict[str, Any]:
    shard_dir = Path(shard_dir)
    meta_db = Path(meta_db)
    if not meta_db.is_file():
        raise PolicyIntegrityError(f"v2 metadata database not found: {meta_db}")
    meta_uri = Path(meta_db).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(meta_uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        package_row = conn.execute("SELECT * FROM policy_package WHERE id = 1").fetchone()
        if package_row is None:
            raise PolicyIntegrityError("v2 policy_package row is missing")
        package = dict(package_row)
        shard_rows = [dict(row) for row in conn.execute("SELECT * FROM shards ORDER BY shard_id")]
        tombstone_rows = [dict(row) for row in conn.execute("SELECT * FROM tombstones ORDER BY policy_key")]
    finally:
        conn.close()

    records: list[dict[str, Any]] = []
    shard_hashes: dict[str, str] = {}
    for shard_row in shard_rows:
        shard_id = shard_row["shard_id"]
        shard_path = shard_dir / shard_row["path"]
        if not shard_path.is_file():
            raise PolicyIntegrityError(f"v2 shard missing: {shard_path}")
        actual_hash = sha256_file(shard_path)
        if actual_hash != shard_row["sha256"]:
            raise PolicyIntegrityError(
                f"v2 shard hash mismatch for {shard_id}",
                {"expected": shard_row["sha256"], "actual": actual_hash},
            )
        shard_hashes[shard_id] = actual_hash
        shard_data = load_json(shard_path)
        if not isinstance(shard_data, dict) or shard_data.get("format") != "v2":
            raise ValidationError(f"v2 shard {shard_id} must have format=v2")
        for envelope in shard_data.get("records", []):
            records.append(
                {
                    "policy_key": envelope["policy_key"],
                    "value": envelope.get("value"),
                    "tombstone": False,
                    "source_format": "v2",
                    "source_priority": SOURCE_PRIORITY["v2"],
                    "source_file": str(shard_path),
                    "source_shard_id": shard_id,
                    "source_package_version": package.get("package_version", "v2"),
                    "declared_clock_ms": int(envelope["declared_clock_ms"]),
                    "generation": int(envelope.get("generation", package["generation"])),
                    "absolute_effective_ms": int(envelope["effective_at_ms"]),
                    "original_time_representation": {
                        "kind": "absolute_effective_milliseconds",
                        "value": int(envelope["effective_at_ms"]),
                    },
                    "time_conversion": {
                        "kind": "absolute_ms_no_conversion",
                        "absolute_effective_ms": int(envelope["effective_at_ms"]),
                        "rounding_direction": "exact",
                        "rounding_remainder_ms": "0",
                    },
                }
            )

    for row in tombstone_rows:
        records.append(
            {
                "policy_key": row["policy_key"],
                "value": None,
                "tombstone": True,
                "source_format": "v2",
                "source_priority": SOURCE_PRIORITY["v2"],
                "source_file": str(meta_db),
                "source_package_version": package.get("package_version", "v2"),
                "declared_clock_ms": int(row["declared_clock_ms"]),
                "generation": int(row["generation"]),
                "absolute_effective_ms": int(row["absolute_effective_ms"]),
                "original_time_representation": {
                    "kind": "tombstone_absolute_milliseconds",
                    "value": int(row["absolute_effective_ms"]),
                },
                "time_conversion": {
                    "kind": "absolute_ms_no_conversion",
                    "absolute_effective_ms": int(row["absolute_effective_ms"]),
                    "rounding_direction": "exact",
                },
                "tombstone_reason": row.get("reason"),
            }
        )

    package["shard_hashes_verified"] = shard_hashes
    return {"format": "v2", "path": str(meta_db), "package": package, "records": records}


def _winner(candidates: list[dict[str, Any]]) -> tuple[dict[str, Any], list[str]]:
    ordered = sorted(
        candidates,
        key=lambda item: (
            int(item["declared_clock_ms"]),
            int(item["source_priority"]),
            int(item["generation"]),
        ),
        reverse=True,
    )
    winner = ordered[0]
    runner_up = ordered[1] if len(ordered) > 1 else None
    reasons: list[str] = []
    if runner_up is not None:
        if winner["declared_clock_ms"] != runner_up["declared_clock_ms"]:
            reasons.append(
                "higher_declared_logical_clock:"
                f"{winner['declared_clock_ms']}>{runner_up['declared_clock_ms']}"
            )
        elif winner["source_format"] != runner_up["source_format"]:
            reasons.append(
                "equal_logical_clock_source_priority:"
                f"{winner['source_format']}>{runner_up['source_format']}"
            )
            if winner["generation"] != runner_up["generation"]:
                reasons.append(
                    "generation_retained_but_source_tiebreak_precedes:"
                    f"winner_generation={winner['generation']} loser_generation={runner_up['generation']}"
                )
        else:
            reasons.append(
                "same_source_higher_generation:"
                f"{winner['generation']}>={runner_up['generation']}"
            )
    else:
        reasons.append("single_source_record")
    reasons.append(f"policy_generation={winner['generation']}")
    return winner, reasons


def merge_policies(
    v1_bundle: dict[str, Any],
    v2_bundle: dict[str, Any],
    *,
    object_set_digest: str | None = None,
) -> dict[str, Any]:
    by_key: dict[str, list[dict[str, Any]]] = {}
    for record in v1_bundle["records"] + v2_bundle["records"]:
        by_key.setdefault(record["policy_key"], []).append(record)

    entries: dict[str, dict[str, Any]] = {}
    adjudications: list[dict[str, Any]] = []
    tombstones: dict[str, dict[str, Any]] = {}
    for key in sorted(by_key):
        candidates = by_key[key]
        winner, reasons = _winner(candidates)
        shared = len({candidate["source_format"] for candidate in candidates}) > 1
        adjudication = {
            "policy_key": key,
            "shared_across_formats": shared,
            "candidate_count": len(candidates),
            "candidates": [
                {
                    "source_format": candidate["source_format"],
                    "source_package_version": candidate["source_package_version"],
                    "declared_clock_ms": candidate["declared_clock_ms"],
                    "generation": candidate["generation"],
                    "tombstone": candidate["tombstone"],
                    "absolute_effective_ms": candidate["absolute_effective_ms"],
                }
                for candidate in sorted(
                    candidates,
                    key=lambda item: (item["source_format"], item["declared_clock_ms"]),
                )
            ],
            "winner_source_format": winner["source_format"],
            "winner_generation": winner["generation"],
            "winner_declared_clock_ms": winner["declared_clock_ms"],
            "decision_basis": reasons,
        }
        adjudications.append(adjudication)
        if winner["tombstone"]:
            tombstones[key] = {
                "policy_key": key,
                "source_format": winner["source_format"],
                "declared_clock_ms": winner["declared_clock_ms"],
                "generation": winner["generation"],
                "reason": winner.get("tombstone_reason"),
                "decision_basis": reasons,
            }
            continue
        entries[key] = {
            "policy_key": key,
            "value": winner["value"],
            "source_version": winner["source_package_version"],
            "source_format": winner["source_format"],
            "declared_clock_ms": winner["declared_clock_ms"],
            "generation": winner["generation"],
            "absolute_effective_ms": winner["absolute_effective_ms"],
            "original_time_representation": winner["original_time_representation"],
            "time_conversion": winner["time_conversion"],
            "decision_basis": reasons,
        }

    v2_package = v2_bundle["package"]
    shard_hashes = dict(v2_package.get("shard_hashes_verified", {}))
    shard_hashes[f"v1:{Path(v1_bundle['path']).name}"] = v1_bundle["file_sha256"]
    package = {
        "manifest_version": int(v2_package["policy_manifest_version"]),
        "generation": int(v2_package["generation"]),
        "generation_floor": int(v2_package.get("generation_floor", v2_package["generation"])),
        "package_version": v2_package.get("package_version", "merged"),
        "shard_hashes": shard_hashes,
        "object_set_digest": object_set_digest,
        "entries": entries,
        "tombstones": tombstones,
        "adjudications": adjudications,
        "source_hashes": {
            "v1_file": v1_bundle["file_sha256"],
            "v2_meta_db": sha256_file(v2_bundle["path"]),
            "shards": shard_hashes,
        },
    }
    package["snapshot_digest"] = sha256_json(
        {
            "manifest_version": package["manifest_version"],
            "generation": package["generation"],
            "object_set_digest": object_set_digest,
            "entries": entries,
            "tombstones": tombstones,
        }
    )
    package["summary"] = {
        "total_keys_seen": len(by_key),
        "active_keys": len(entries),
        "tombstoned_keys": len(tombstones),
        "shared_keys": sum(1 for item in adjudications if item["shared_across_formats"]),
        "v1_winners": sum(1 for item in entries.values() if item["source_format"] == "v1"),
        "v2_winners": sum(1 for item in entries.values() if item["source_format"] == "v2"),
    }
    return package
