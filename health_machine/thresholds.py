"""Validation, cross-version merging and persistence of threshold views."""
from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Any

from .errors import (
    InvalidTombstone,
    MergeError,
    NoActiveThreshold,
    V1ThresholdLengthMismatch,
    V1ThresholdValueInvalid,
    V1TimeValueInvalid,
    V1UnknownInstance,
    V2GenerationNotMonotonic,
    V2ShardHashMismatch,
    V2ShardMissing,
)
from .engine import fault_point
from .store import Store
from .util import canonical_json, is_finite_number, is_real_int, read_json, sha256_file, sha256_json

FORMAT_PRIORITY = {"v1": 1, "v2": 2}
DEFAULT_STATE_POLICY = {
    "healthy_score_bp": 8500,
    "degraded_after_failures": 1,
    "lost_after_failures": 2,
    "isolated_after_failures": 4,
    "degraded_recovery_windows": 2,
    "degraded_recovery_duration_ms": 1000,
    "lost_recovery_windows": 3,
    "lost_recovery_duration_ms": 2000,
    "isolated_recovery_windows": 4,
    "isolated_recovery_duration_ms": 3000,
}


def _integer_in_range(value: Any, *, minimum: int = 0, maximum: int | None = None) -> bool:
    if not is_real_int(value) or value < minimum:
        return False
    return maximum is None or value <= maximum


def _mapping_value(value: Any, instance_id: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(instance_id, default)
    return value if value is not None else default


def convert_v1_seconds(raw_seconds: Any, time_base_ms: int) -> dict[str, Any]:
    if not is_finite_number(raw_seconds):
        raise V1TimeValueInvalid(
            "effective_after_seconds must be a finite non-negative number",
            details={"value": str(raw_seconds)},
        )
    seconds = Decimal(str(raw_seconds)) if not isinstance(raw_seconds, Decimal) else raw_seconds
    if seconds < 0:
        raise V1TimeValueInvalid("effective_after_seconds must be non-negative")
    absolute_offset_ms = int(seconds * Decimal(1000).to_integral_value(rounding="ROUND_FLOOR"))
    return {
        "raw_value": str(raw_seconds),
        "raw_unit": "seconds",
        "scale_to_ms": 1000,
        "rounding": "floor",
        "time_base_ms": time_base_ms,
        "absolute_effective_at_ms": time_base_ms + absolute_offset_ms,
    }


def validate_load_v1(path: str | Path, registry: dict[str, Any]) -> dict[str, Any]:
    path = Path(path)
    doc = read_json(path)
    if not isinstance(doc, dict):
        raise V1ThresholdValueInvalid("v1 threshold snapshot must be a JSON object", location={"path": str(path)})

    snapshot_id = doc.get("snapshot_id", path.stem)
    generation = doc.get("threshold_generation", 1)
    logical_clock_ms = doc.get("logical_clock_ms", 0)
    time_base_ms = doc.get("time_base_ms", 0)
    if not _integer_in_range(generation, minimum=0):
        raise V1ThresholdValueInvalid("threshold_generation must be a non-negative integer", location={"path": "threshold_generation"})
    if not _integer_in_range(logical_clock_ms, minimum=0):
        raise V1ThresholdValueInvalid("logical_clock_ms must be a non-negative integer", location={"path": "logical_clock_ms"})
    if not _integer_in_range(time_base_ms, minimum=0):
        raise V1TimeValueInvalid("time_base_ms must be a non-negative integer", location={"path": "time_base_ms"})

    raw_thresholds = doc.get("failure_thresholds", doc.get("thresholds"))
    if not isinstance(raw_thresholds, dict):
        raise V1ThresholdValueInvalid("v1 failure_thresholds must be an object keyed by instance_id")

    candidates: list[dict[str, Any]] = []
    for instance_id, instance in registry["instances"].items():
        if instance_id not in raw_thresholds:
            continue
        raw_array = raw_thresholds[instance_id]
        if instance_id not in registry["instances"]:
            raise V1UnknownInstance(f"unknown threshold instance {instance_id!r}", location={"instance_id": instance_id})
        levels = instance["levels"]
        if not isinstance(raw_array, list) or len(raw_array) != len(levels):
            raise V1ThresholdLengthMismatch(
                "v1 threshold array length must equal declared level count",
                location={"instance_id": instance_id},
                details={"expected": len(levels), "actual": None if not isinstance(raw_array, list) else len(raw_array)},
            )
        for index, value in enumerate(raw_array):
            level = levels[index]
            if not _integer_in_range(value, minimum=0):
                raise V1ThresholdValueInvalid(
                    "v1 threshold entries must be non-negative integers",
                    location={"instance_id": instance_id, "level": level, "path": f"failure_thresholds[{index}]"},
                )

        raw_weights = _mapping_value(doc.get("weights"), instance_id)
        if raw_weights is not None:
            if not isinstance(raw_weights, list) or len(raw_weights) != len(levels):
                raise V1ThresholdValueInvalid(
                    "weights length must equal declared level count",
                    location={"instance_id": instance_id},
                )
            for index, value in enumerate(raw_weights):
                if not _integer_in_range(value, minimum=1):
                    raise V1ThresholdValueInvalid(
                        "weight entries must be positive integers",
                        location={"instance_id": instance_id, "level": levels[index]},
                    )

        raw_min_scores = _mapping_value(doc.get("min_score_bp"), instance_id, [7000] * len(levels))
        raw_latency_budgets = _mapping_value(doc.get("latency_budget_ms"), instance_id, [100] * len(levels))
        effective_seconds = _mapping_value(doc.get("effective_after_seconds", 0), instance_id, 0)
        time_meta = convert_v1_seconds(effective_seconds, time_base_ms)

        for index, level in enumerate(levels):
            min_score = raw_min_scores[index] if isinstance(raw_min_scores, list) else raw_min_scores
            latency_budget = raw_latency_budgets[index] if isinstance(raw_latency_budgets, list) else raw_latency_budgets
            weight = raw_weights[index] if isinstance(raw_weights, list) else max(1, len(levels) - index)
            if not _integer_in_range(min_score, minimum=0, maximum=10000):
                raise V1ThresholdValueInvalid(
                    "min_score_bp must be an integer in 0..10000",
                    location={"instance_id": instance_id, "level": level},
                )
            if not is_finite_number(latency_budget) or Decimal(str(latency_budget)) < 0:
                raise V1ThresholdValueInvalid(
                    "latency_budget_ms must be a finite non-negative number",
                    location={"instance_id": instance_id, "level": level},
                )
            candidates.append(
                {
                    "instance_id": instance_id,
                    "level": level,
                    "source_format": "v1",
                    "source_snapshot_id": snapshot_id,
                    "generation": generation,
                    "logical_clock_ms": logical_clock_ms,
                    "effective_at_ms": time_meta["absolute_effective_at_ms"],
                    "weight": weight,
                    "failure_limit": raw_array[index],
                    "min_score_bp": min_score,
                    "latency_budget_ms": Decimal(str(latency_budget)),
                    "failure_counts": list(raw_array),
                    "time_conversion": time_meta,
                    "payload": {"threshold_index": index, "raw_value": raw_array[index]},
                }
            )

    unknown = sorted(set(raw_thresholds) - set(registry["instances"]))
    if unknown:
        raise V1UnknownInstance(f"v1 thresholds reference unknown instances: {unknown}", details={"instance_ids": unknown})

    return {
        "path": str(path),
        "doc": doc,
        "snapshot_id": snapshot_id,
        "content_hash": sha256_json(doc),
        "candidates": candidates,
        "state_policy": {**DEFAULT_STATE_POLICY, **(doc.get("state_policy") if isinstance(doc.get("state_policy"), dict) else {})},
    }


def load_v2_bundle(shard_dir: str | Path, meta_db: str | Path | None = None, registry: dict[str, Any] | None = None) -> dict[str, Any]:
    shard_dir = Path(shard_dir)
    meta_path = Path(meta_db) if meta_db else shard_dir / "meta.sqlite"
    if not meta_path.exists():
        raise V2ShardMissing(f"v2 metadata database does not exist: {meta_path}", location={"path": str(meta_path)})

    conn = sqlite3.connect(f"file:{meta_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        try:
            shard_rows = conn.execute("SELECT * FROM v2_shards ORDER BY shard_order, shard_id").fetchall()
        except sqlite3.Error as exc:
            raise V2ShardMissing(f"cannot read v2_shards metadata: {exc}", location={"path": str(meta_path)}) from exc

        candidates: list[dict[str, Any]] = []
        shards: list[dict[str, Any]] = []
        previous_generation: int | None = None
        for row in shard_rows:
            row_dict = dict(row)
            if previous_generation is not None and row_dict["threshold_generation"] < previous_generation:
                raise V2GenerationNotMonotonic(
                    "v2 shard threshold_generation decreases in shard order",
                    details={"shard_id": row_dict["shard_id"], "previous": previous_generation,
                             "actual": row_dict["threshold_generation"]},
                )
            previous_generation = row_dict["threshold_generation"]
            shard_path = shard_dir / row_dict["relative_path"]
            if not shard_path.exists():
                raise V2ShardMissing(f"v2 shard file missing: {shard_path}", details={"shard_id": row_dict["shard_id"]})
            actual_hash = sha256_file(shard_path)
            if actual_hash != row_dict["sha256"]:
                raise V2ShardHashMismatch(
                    "v2 shard sha256 does not match metadata",
                    location={"path": str(shard_path)},
                    details={"shard_id": row_dict["shard_id"], "expected": row_dict["sha256"], "actual": actual_hash},
                )
            shard_doc = read_json(shard_path)
            shards.append(shard_doc)
            for entry in shard_doc.get("thresholds", []) if isinstance(shard_doc, dict) else []:
                instance_id = entry.get("instance_id")
                level = entry.get("level")
                if registry is not None:
                    instance = registry["instances"].get(instance_id) if isinstance(instance_id, str) else None
                    if instance is None:
                        from .errors import UnknownObservationInstance

                        raise UnknownObservationInstance(
                            f"v2 threshold references unknown instance {instance_id!r}",
                            location={"instance_id": instance_id if isinstance(instance_id, str) else None},
                        )
                    if not _integer_in_range(level, minimum=1) or level not in instance["levels"]:
                        from .errors import UnknownObservationLevel

                        raise UnknownObservationLevel(
                            f"v2 threshold references undeclared level {level!r}",
                            location={"instance_id": instance_id, "level": level if is_real_int(level) else None},
                        )
                for field in ("generation", "logical_clock_ms", "effective_at_ms", "weight", "failure_limit", "min_score_bp"):
                    value = entry.get(field)
                    minimum = 1 if field == "weight" else 0
                    maximum = 10000 if field == "min_score_bp" else None
                    if not _integer_in_range(value, minimum=minimum, maximum=maximum):
                        raise V2GenerationNotMonotonic(
                            f"v2 threshold field {field} is invalid",
                            location={"instance_id": instance_id, "level": level if is_real_int(level) else None},
                        )
                latency_budget = entry.get("latency_budget_ms", 100)
                if not is_finite_number(latency_budget) or Decimal(str(latency_budget)) < 0:
                    raise V2GenerationNotMonotonic(
                        "v2 latency_budget_ms must be finite and non-negative",
                        location={"instance_id": instance_id, "level": level},
                    )
                failure_counts = entry.get("failure_counts", [entry["failure_limit"]])
                if not isinstance(failure_counts, list) or any(not _integer_in_range(item) for item in failure_counts):
                    raise V2GenerationNotMonotonic(
                        "v2 failure_counts must be a list of non-negative integers",
                        location={"instance_id": instance_id, "level": level},
                    )
                ledger = conn.execute(
                    "SELECT generation, logical_clock_ms FROM v2_generation_ledger WHERE instance_id=? AND level=? AND generation=?",
                    (instance_id, level, entry["generation"]),
                ).fetchone()
                if ledger is None or ledger["logical_clock_ms"] != entry["logical_clock_ms"]:
                    raise V2GenerationNotMonotonic(
                        "v2 threshold generation is absent from monotonic ledger or clock differs",
                        location={"instance_id": instance_id, "level": level},
                        details={"generation": entry["generation"]},
                    )
                candidates.append(
                    {
                        "instance_id": instance_id,
                        "level": level,
                        "source_format": "v2",
                        "source_snapshot_id": row_dict["shard_id"],
                        "generation": entry["generation"],
                        "logical_clock_ms": entry["logical_clock_ms"],
                        "effective_at_ms": entry["effective_at_ms"],
                        "weight": entry["weight"],
                        "failure_limit": entry["failure_limit"],
                        "min_score_bp": entry["min_score_bp"],
                        "latency_budget_ms": Decimal(str(latency_budget)),
                        "failure_counts": failure_counts,
                        "time_conversion": {
                            "raw_value": str(entry["effective_at_ms"]),
                            "raw_unit": "milliseconds",
                            "scale_to_ms": 1,
                            "rounding": "identity",
                            "absolute_effective_at_ms": entry["effective_at_ms"],
                        },
                        "payload": entry,
                    }
                )

        tombstones: list[dict[str, Any]] = []
        tomb_rows = conn.execute("SELECT * FROM v2_tombstones ORDER BY instance_id, level, generation").fetchall()
        grouped: dict[tuple[str, int], list[sqlite3.Row]] = {}
        for row in tomb_rows:
            grouped.setdefault((row["instance_id"], row["level"]), []).append(row)
        for (instance_id, level), rows in grouped.items():
            if registry is not None:
                instance = registry["instances"].get(instance_id)
                if instance is None or level not in instance["levels"]:
                    raise InvalidTombstone(
                        "tombstone targets unknown instance or level",
                        location={"instance_id": instance_id, "level": level},
                    )
            last_generation = -1
            for row in rows:
                tomb = dict(row)
                if not _integer_in_range(tomb.get("generation"), minimum=1):
                    raise InvalidTombstone("tombstone generation must be a positive integer", location={"instance_id": instance_id, "level": level})
                if tomb["generation"] <= last_generation:
                    raise InvalidTombstone("tombstone generations must be strictly increasing", location={"instance_id": instance_id, "level": level})
                last_generation = tomb["generation"]
                if not _integer_in_range(tomb.get("deleted_at_ms"), minimum=0):
                    raise InvalidTombstone("deleted_at_ms must be a non-negative integer", location={"instance_id": instance_id, "level": level})
                if not isinstance(tomb.get("reason"), str) or not tomb["reason"].strip():
                    raise InvalidTombstone("tombstone reason must be non-empty", location={"instance_id": instance_id, "level": level})
                active_candidates = [c for c in candidates if c["instance_id"] == instance_id and c["level"] == level]
                latest_generation = max((c["generation"] for c in active_candidates), default=0)
                if tomb["generation"] < latest_generation:
                    raise InvalidTombstone(
                        "tombstone generation cannot be older than active threshold generation",
                        location={"instance_id": instance_id, "level": level},
                        details={"tombstone_generation": tomb["generation"], "threshold_generation": latest_generation},
                    )
                tombstones.append(tomb)
    finally:
        conn.close()

    return {
        "meta_path": str(meta_path),
        "candidates": candidates,
        "tombstones": tombstones,
        "shards": shards,
        "content_hash": sha256_json({"candidates": candidates, "tombstones": tombstones}),
    }


def _candidate_rank(candidate: dict[str, Any]) -> tuple[int, int, int]:
    return (
        candidate["logical_clock_ms"],
        FORMAT_PRIORITY[candidate["source_format"]],
        candidate["generation"],
    )


def build_unified_view(
    registry: dict[str, Any],
    v1: dict[str, Any] | None,
    v2: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    candidates = list((v1 or {}).get("candidates", [])) + list((v2 or {}).get("candidates", []))
    tombstones = list((v2 or {}).get("tombstones", []))
    views: list[dict[str, Any]] = []

    for instance_id, instance in registry["instances"].items():
        for level in instance["levels"]:
            level_candidates = [c for c in candidates if c["instance_id"] == instance_id and c["level"] == level]
            level_tombs = [t for t in tombstones if t["instance_id"] == instance_id and t["level"] == level]
            level_tomb = max(level_tombs, key=lambda t: (t["deleted_at_ms"], t["generation"]), default=None)
            if not level_candidates and level_tomb is None:
                views.append(
                    {
                        "instance_id": instance_id,
                        "level": level,
                        "active": False,
                        "exclusion_reason": "unmigrated_threshold",
                        "excluded_generation": None,
                        "generation": 0,
                        "logical_clock_ms": 0,
                        "effective_at_ms": 0,
                        "weight": 0,
                        "failure_limit": 0,
                        "min_score_bp": 0,
                        "latency_budget_ms": Decimal(0),
                        "failure_counts": [],
                        "time_conversion": {},
                        "winner_format": "none",
                        "winner_snapshot_id": "none",
                        "decision": {"winner": None, "reason": "no_v1_or_v2_threshold"},
                        "provenance": {"candidates": []},
                        "content_hash": sha256_json({"instance_id": instance_id, "level": level, "active": False}),
                    }
                )
                continue

            winner = max(level_candidates, key=_candidate_rank) if level_candidates else None
            decision: dict[str, Any]
            if winner is None:
                decision = {"winner": None, "reason": "tombstone_without_candidate"}
                view = {
                    "weight": 0,
                    "failure_limit": 0,
                    "min_score_bp": 0,
                    "latency_budget_ms": Decimal(0),
                    "failure_counts": [],
                    "time_conversion": {},
                    "logical_clock_ms": level_tomb["deleted_at_ms"],
                    "effective_at_ms": level_tomb["deleted_at_ms"],
                    "generation": level_tomb["generation"],
                    "winner_format": "tombstone",
                    "winner_snapshot_id": "v2-tombstone",
                }
            else:
                losers = []
                reason = "only_source"
                all_candidates = level_candidates
                for candidate in level_candidates:
                    if candidate is winner:
                        continue
                    losers.append(
                        {
                            "source_format": candidate["source_format"],
                            "snapshot_id": candidate["source_snapshot_id"],
                            "generation": candidate["generation"],
                            "logical_clock_ms": candidate["logical_clock_ms"],
                            "rank": list(_candidate_rank(candidate)),
                        }
                    )
                if len(all_candidates) > 1:
                    if any(candidate["logical_clock_ms"] != winner["logical_clock_ms"] for candidate in all_candidates):
                        reason = "newer_logical_clock"
                    elif any(candidate["source_format"] != winner["source_format"] for candidate in all_candidates):
                        same_generation = all(candidate["generation"] == winner["generation"] for candidate in all_candidates)
                        reason = (
                            "same_clock_same_generation_format_priority"
                            if same_generation
                            else "same_clock_format_priority"
                        )
                    elif any(candidate["generation"] != winner["generation"] for candidate in all_candidates):
                        reason = "same_clock_higher_generation"
                decision = {
                    "reason": reason,
                    "winner_rank": list(_candidate_rank(winner)),
                    "winner": {
                        "source_format": winner["source_format"],
                        "snapshot_id": winner["source_snapshot_id"],
                        "generation": winner["generation"],
                    },
                    "losers": losers,
                }
                view = {key: winner[key] for key in (
                    "weight", "failure_limit", "min_score_bp", "latency_budget_ms",
                    "failure_counts", "time_conversion", "logical_clock_ms", "effective_at_ms", "generation",
                )}
                view["winner_format"] = winner["source_format"]
                view["winner_snapshot_id"] = winner["source_snapshot_id"]

            active = True
            exclusion_reason = None
            excluded_generation = None
            if level_tomb is not None:
                tomb_rank = (level_tomb["deleted_at_ms"], FORMAT_PRIORITY["v2"], level_tomb["generation"])
                winner_rank = _candidate_rank(winner) if winner else (-1, -1, -1)
                if tomb_rank >= winner_rank:
                    active = False
                    exclusion_reason = "tombstoned"
                    excluded_generation = level_tomb["generation"]
                    decision["tombstone"] = {
                        "generation": level_tomb["generation"],
                        "deleted_at_ms": level_tomb["deleted_at_ms"],
                        "reason": level_tomb["reason"],
                    }
            if active and winner is None:
                active = False
                exclusion_reason = "unmigrated_threshold"
            view.update(
                {
                    "instance_id": instance_id,
                    "level": level,
                    "active": active,
                    "exclusion_reason": exclusion_reason,
                    "excluded_generation": excluded_generation,
                    "decision": decision,
                    "provenance": {
                        "winner": winner,
                        "tombstones": level_tombs,
                        "candidate_count": len(level_candidates),
                        "format_priority": FORMAT_PRIORITY,
                    },
                }
            )
            view["content_hash"] = sha256_json(
                {
                    "instance_id": instance_id,
                    "level": level,
                    "active": active,
                    "generation": view["generation"],
                    "weight": view["weight"],
                    "failure_limit": view["failure_limit"],
                    "min_score_bp": view["min_score_bp"],
                    "latency_budget_ms": str(view["latency_budget_ms"]),
                    "failure_counts": view["failure_counts"],
                    "decision": decision,
                }
            )
            views.append(view)

    for instance_id in registry["instances"]:
        active_views = [v for v in views if v["instance_id"] == instance_id and v["active"] and v["weight"] > 0]
        if not active_views:
            raise NoActiveThreshold(
                "instance has no active weighted threshold after merge",
                location={"instance_id": instance_id},
                details={
                    "excluded": [
                        {"level": v["level"], "generation": v.get("excluded_generation", v["generation"]),
                         "reason": v["exclusion_reason"]}
                        for v in views if v["instance_id"] == instance_id
                    ]
                },
            )
    return views


def persist_merge(
    store: Store,
    registry: dict[str, Any],
    v1: dict[str, Any] | None,
    v2: dict[str, Any] | None,
    views: list[dict[str, Any]],
    *,
    merge_version: str = "fixed-sample",
) -> dict[str, Any]:
    state_policy = (v1 or {}).get("state_policy", DEFAULT_STATE_POLICY)
    merge_hash = sha256_json({"v1": (v1 or {}).get("content_hash"), "v2": (v2 or {}).get("content_hash"), "views": [(v["instance_id"], v["level"], v["content_hash"]) for v in views]})
    with store.transaction("_snapshot") as conn:
        existing = conn.execute("SELECT content_hash FROM snapshot_versions WHERE snapshot_version=?", (merge_version,)).fetchone()
        if existing is not None:
            if existing["content_hash"] != merge_hash:
                from .errors import ConcurrencyError

                raise ConcurrencyError(
                    "threshold snapshot version already exists with different content",
                    details={"snapshot_version": merge_version},
                    code="HSM-7002",
                )
            return {"changed": False, "merge_version": merge_version, "merge_hash": merge_hash, "views": len(views)}

        if v1:
            doc = v1["doc"]
            conn.execute(
                "INSERT OR REPLACE INTO threshold_snapshots VALUES (?, ?, ?, ?, ?, ?, ?)",
                (v1["snapshot_id"], "v1", doc.get("logical_clock_ms", 0), doc.get("threshold_generation", 1), v1["path"], v1["content_hash"], canonical_json(doc)),
            )
        if v2:
            conn.execute(
                "INSERT OR REPLACE INTO threshold_snapshots VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("v2-bundle", "v2", 0, 0, v2["meta_path"], v2["content_hash"], canonical_json({"shards": v2.get("shards", [])})),
            )
        for candidate in (v1 or {}).get("candidates", []) + (v2 or {}).get("candidates", []):
            conn.execute(
                """
                INSERT OR REPLACE INTO threshold_candidates
                (instance_id, level, source_format, source_snapshot_id, generation, logical_clock_ms,
                 effective_at_ms, weight, failure_limit, min_score_bp, latency_budget_ms,
                 failure_counts_json, time_conversion_json, payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    candidate["instance_id"], candidate["level"], candidate["source_format"], candidate["source_snapshot_id"],
                    candidate["generation"], candidate["logical_clock_ms"], candidate["effective_at_ms"], candidate["weight"],
                    candidate["failure_limit"], candidate["min_score_bp"], str(candidate["latency_budget_ms"]),
                    canonical_json(candidate["failure_counts"]), canonical_json(candidate["time_conversion"]),
                    canonical_json(candidate["payload"]),
                ),
            )

        for view in views:
            iid = view["instance_id"]
            revision = 1
            conn.execute(
                """
                INSERT OR REPLACE INTO threshold_view
                (instance_id, level, winner_format, winner_snapshot_id, generation, logical_clock_ms,
                 effective_at_ms, weight, failure_limit, min_score_bp, latency_budget_ms, active,
                 exclusion_reason, excluded_generation, raw_failure_counts_json, time_conversion_json,
                 provenance_json, decision_json, view_revision, content_hash, updated_patch_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    iid, view["level"], view["winner_format"], view["winner_snapshot_id"], view["generation"],
                    view["logical_clock_ms"], view["effective_at_ms"], view["weight"], view["failure_limit"],
                    view["min_score_bp"], str(view["latency_budget_ms"]), 1 if view["active"] else 0,
                    view["exclusion_reason"], view["excluded_generation"], canonical_json(view["failure_counts"]),
                    canonical_json(view["time_conversion"]), canonical_json(_json_safe(view["provenance"])),
                    canonical_json(view["decision"]), revision, view["content_hash"], None,
                ),
            )
            max_generation = max(
                [view["generation"]]
                + [c["generation"] for c in (v1 or {}).get("candidates", []) if c["instance_id"] == iid]
                + [t["generation"] for t in (v2 or {}).get("tombstones", []) if t["instance_id"] == iid]
            )
            pointer_hash = sha256_json([v["content_hash"] for v in views if v["instance_id"] == iid])
            conn.execute(
                """
                INSERT INTO unified_pointer(instance_id, view_revision, view_hash, threshold_generation, updated_patch_id, updated_clock_ms)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(instance_id) DO UPDATE SET
                    view_revision=excluded.view_revision,
                    view_hash=excluded.view_hash,
                    threshold_generation=excluded.threshold_generation,
                    updated_patch_id=excluded.updated_patch_id,
                    updated_clock_ms=excluded.updated_clock_ms
                """,
                (iid, revision, pointer_hash, max_generation, None, 0),
            )
        conn.execute("INSERT INTO snapshot_versions(snapshot_version, kind, content_hash, payload_json) VALUES (?, ?, ?, ?)",
                     (merge_version, "merge", merge_hash, canonical_json({"state_policy": state_policy})))
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('state_policy', ?)", (canonical_json(state_policy),))
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('merge_version', ?)", (merge_version,))
        fault_point("before_merge_commit")
        return {"changed": True, "merge_version": merge_version, "merge_hash": merge_hash, "views": len(views)}


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items() if key != "winner" or True}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, Decimal):
        return str(value)
    return value
