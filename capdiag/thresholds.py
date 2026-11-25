from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any

from .errors import CapDiagError, ErrorCode
from .fixedpoint import fraction_decimal
from .jsonutil import load_json, sha256_file
from .validation import parse_decimal, require_nonnegative_int, require_positive_int


SOURCE_PRIORITY = {"v1": 1, "v2": 2}


@dataclass(frozen=True)
class ThresholdCandidate:
    scope: str
    metric: str
    level: str
    value: Decimal
    effective_logical_ms: int
    generation: int
    source_version: str
    snapshot_id: str
    original_time_repr: dict[str, Any]
    tombstone: bool = False

    def key(self) -> tuple[str, str, str]:
        return (self.scope, self.metric, self.level)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "metric": self.metric,
            "level": self.level,
            "value": format(self.value, "f"),
            "effective_logical_ms": self.effective_logical_ms,
            "generation": self.generation,
            "source_version": self.source_version,
            "snapshot_id": self.snapshot_id,
            "original_time_repr": self.original_time_repr,
            "tombstone": self.tombstone,
        }


def _split_flat_key(full_key: str) -> tuple[str, str, str]:
    parts = full_key.split(":")
    if len(parts) != 3:
        raise CapDiagError(
            ErrorCode.THRESHOLD_REFERENCE,
            "flat threshold key must be scope:metric:level",
            {"key": full_key},
        )
    scope, metric, level = parts
    if level not in {"warn", "fail"}:
        raise CapDiagError(
            ErrorCode.THRESHOLD_REFERENCE,
            "threshold level must be warn or fail",
            {"key": full_key, "level": level},
        )
    return scope, metric, level


def validate_metric_references(metric_names: set[str], candidates: list[ThresholdCandidate]) -> None:
    unknown = sorted({candidate.metric for candidate in candidates if candidate.metric not in metric_names})
    if unknown:
        raise CapDiagError(
            ErrorCode.THRESHOLD_REFERENCE,
            "threshold snapshot references undefined metrics",
            {"unknown_metrics": unknown, "defined_metrics": sorted(metric_names)},
        )


def load_v1_thresholds(path: str | Path, *, origin_logical_ms: int = 0) -> list[ThresholdCandidate]:
    data = load_json(path)
    if not isinstance(data, dict):
        raise CapDiagError(ErrorCode.SCHEMA, "v1 threshold snapshot must be an object")

    relative_seconds = require_nonnegative_int(
        data.get("effective_relative_seconds"),
        "effective_relative_seconds",
    )
    generation = data.get("threshold_generation", 1)
    generation = require_positive_int(generation, "threshold_generation")
    snapshot_id = str(data.get("snapshot_id", Path(path).stem))
    flat = data.get("thresholds")
    if not isinstance(flat, dict) or not flat:
        raise CapDiagError(ErrorCode.SCHEMA, "v1 thresholds must be a non-empty flat object")

    ms_per_second = 1000
    effective_ms = origin_logical_ms + relative_seconds * ms_per_second
    candidates: list[ThresholdCandidate] = []
    for full_key, raw_value in flat.items():
        scope, metric, level = _split_flat_key(full_key)
        value = parse_decimal(raw_value, field_name=full_key)
        candidates.append(
            ThresholdCandidate(
                scope=scope,
                metric=metric,
                level=level,
                value=value,
                effective_logical_ms=effective_ms,
                generation=generation,
                source_version="v1",
                snapshot_id=snapshot_id,
                original_time_repr={
                    "effective_relative_seconds": relative_seconds,
                    "origin_logical_ms": origin_logical_ms,
                    "conversion": {
                        "scale_ms_per_second": ms_per_second,
                        "formula": "origin_logical_ms + effective_relative_seconds * 1000",
                        "rounding_direction": "exact_integer_multiplication",
                        "converted_absolute_ms": effective_ms,
                    },
                },
            )
        )
    return candidates


def _v2_connect(metadata_db: str | Path) -> sqlite3.Connection:
    path = Path(metadata_db)
    if not path.exists():
        raise CapDiagError(ErrorCode.SCHEMA, f"v2 metadata database does not exist: {path}")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def load_v2_thresholds(
    metadata_db: str | Path,
    *,
    shard_directory: str | Path | None = None,
) -> list[ThresholdCandidate]:
    metadata_db = Path(metadata_db)
    base_dir = Path(shard_directory) if shard_directory is not None else metadata_db.parent
    with _v2_connect(metadata_db) as conn:
        snapshot_row = conn.execute(
            "SELECT snapshot_id, generation, effective_absolute_ms FROM snapshot ORDER BY generation DESC LIMIT 1"
        ).fetchone()
        if snapshot_row is None:
            raise CapDiagError(ErrorCode.SCHEMA, "v2 metadata snapshot table is empty")
        snapshot_id = snapshot_row["snapshot_id"]
        generation = require_positive_int(snapshot_row["generation"], "v2.generation")
        effective_ms = require_nonnegative_int(
            snapshot_row["effective_absolute_ms"],
            "v2.effective_absolute_ms",
        )
        rows = conn.execute(
            "SELECT shard_id, path, sha256, generation FROM shards ORDER BY shard_id ASC"
        ).fetchall()

    if not rows:
        raise CapDiagError(ErrorCode.SCHEMA, "v2 metadata contains no shards")

    previous_generation = 0
    candidates: list[ThresholdCandidate] = []
    for row in rows:
        shard_generation = require_positive_int(row["generation"], f"shard_{row['shard_id']}.generation")
        if shard_generation < previous_generation:
            raise CapDiagError(
                ErrorCode.V2_GENERATION,
                "v2 shard generations must be monotonically non-decreasing",
                {
                    "shard_id": row["shard_id"],
                    "previous_generation": previous_generation,
                    "actual_generation": shard_generation,
                },
            )
        previous_generation = shard_generation

        shard_path = Path(row["path"])
        if not shard_path.is_absolute():
            shard_path = base_dir / shard_path
        actual_hash = sha256_file(shard_path)
        if actual_hash != row["sha256"]:
            raise CapDiagError(
                ErrorCode.V2_HASH,
                "v2 shard hash mismatch",
                {
                    "shard_id": row["shard_id"],
                    "path": str(shard_path),
                    "expected_sha256": row["sha256"],
                    "actual_sha256": actual_hash,
                },
            )

        shard = load_json(shard_path)
        if not isinstance(shard, dict):
            raise CapDiagError(ErrorCode.SCHEMA, "v2 shard root must be an object")
        if int(shard.get("shard_id", -1)) != int(row["shard_id"]):
            raise CapDiagError(
                ErrorCode.V2_HASH,
                "v2 shard id does not match metadata",
                {"metadata_shard_id": row["shard_id"], "file_shard_id": shard.get("shard_id")},
            )

        shard_effective = shard.get("effective_absolute_ms", effective_ms)
        shard_effective = require_nonnegative_int(shard_effective, "effective_absolute_ms")
        shard_snapshot = str(shard.get("snapshot_id", snapshot_id))
        original_time = {
            "effective_absolute_ms": shard_effective,
            "metadata_snapshot_effective_absolute_ms": effective_ms,
        }

        for entry in shard.get("entries", []):
            scope = str(entry["scope"])
            metric = str(entry["metric"])
            level = str(entry["level"])
            if level not in {"warn", "fail"}:
                raise CapDiagError(ErrorCode.THRESHOLD_REFERENCE, "invalid threshold level", entry)
            candidates.append(
                ThresholdCandidate(
                    scope=scope,
                    metric=metric,
                    level=level,
                    value=parse_decimal(entry["value"], field_name=f"{scope}:{metric}:{level}"),
                    effective_logical_ms=shard_effective,
                    generation=int(shard.get("generation", shard_generation)),
                    source_version="v2",
                    snapshot_id=shard_snapshot,
                    original_time_repr=original_time,
                )
            )

        for full_key in shard.get("tombstones", []):
            scope, metric, level = _split_flat_key(full_key)
            candidates.append(
                ThresholdCandidate(
                    scope=scope,
                    metric=metric,
                    level=level,
                    value=Decimal(0),
                    effective_logical_ms=shard_effective,
                    generation=int(shard.get("generation", shard_generation)),
                    source_version="v2",
                    snapshot_id=shard_snapshot,
                    original_time_repr=original_time,
                    tombstone=True,
                )
            )

    return candidates


def adjudicate_pair(
    candidates: list[ThresholdCandidate],
) -> tuple[ThresholdCandidate | None, str]:
    active = [candidate for candidate in candidates if not candidate.tombstone]
    tombstones = [candidate for candidate in candidates if candidate.tombstone]
    if tombstones and (not active or max(tombstones, key=lambda c: c.effective_logical_ms).effective_logical_ms >= max(active, key=lambda c: c.effective_logical_ms).effective_logical_ms):
        newest_tombstone = max(tombstones, key=lambda c: (c.effective_logical_ms, c.generation, SOURCE_PRIORITY[c.source_version]))
        return None, f"tombstoned_by_{newest_tombstone.source_version}_gen{newest_tombstone.generation}"
    if not active:
        return None, "tombstoned"

    newest_clock = max(candidate.effective_logical_ms for candidate in active)
    clock_tied = [candidate for candidate in active if candidate.effective_logical_ms == newest_clock]
    if len(clock_tied) == 1:
        winner = clock_tied[0]
        return winner, f"logical_clock_newer:{winner.effective_logical_ms}"

    source_tied = max(clock_tied, key=lambda c: (SOURCE_PRIORITY[c.source_version], c.generation))
    versions = sorted({c.source_version for c in clock_tied})
    if len(versions) > 1:
        basis = f"same_logical_clock_source_priority:{source_tied.source_version}>other"
    else:
        basis = f"same_logical_clock_and_source_generation:{source_tied.generation}"
    return source_tied, basis


def merge_thresholds(
    v1: list[ThresholdCandidate],
    v2: list[ThresholdCandidate],
) -> dict[str, Any]:
    groups: dict[tuple[str, str, str], list[ThresholdCandidate]] = {}
    for candidate in [*v1, *v2]:
        groups.setdefault(candidate.key(), []).append(candidate)

    entries: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    for key in sorted(groups):
        candidates = groups[key]
        winner, basis = adjudicate_pair(candidates)
        decision = {
            "key": ":".join(key),
            "decision": basis,
            "candidates": [candidate.to_dict() for candidate in sorted(candidates, key=lambda c: c.source_version)],
            "winner": winner.to_dict() if winner else None,
        }
        decisions.append(decision)
        if winner is not None:
            entry = winner.to_dict()
            entry["decision_basis"] = basis
            entries.append(entry)

    return {
        "schema": "unified-threshold-view/v1",
        "entry_count": len(entries),
        "entries": entries,
        "decisions": decisions,
    }


def select_thresholds_at_clock(
    candidates: list[ThresholdCandidate],
    clock_ms: int,
) -> dict[tuple[str, str, str], ThresholdCandidate]:
    applicable: dict[tuple[str, str, str], list[ThresholdCandidate]] = {}
    for candidate in candidates:
        if candidate.effective_logical_ms <= clock_ms:
            applicable.setdefault(candidate.key(), []).append(candidate)

    result: dict[tuple[str, str, str], ThresholdCandidate] = {}
    for key, values in applicable.items():
        winner, _basis = adjudicate_pair(values)
        if winner is not None:
            result[key] = winner
    return result


def threshold_for_scope(
    view: dict[tuple[str, str, str], ThresholdCandidate],
    *,
    scope_id: str,
    service_name: str | None,
    metric: str,
) -> dict[str, ThresholdCandidate] | None:
    for scope in (scope_id, service_name, "default"):
        if scope is None:
            continue
        warn = view.get((scope, metric, "warn"))
        fail = view.get((scope, metric, "fail"))
        if warn is not None or fail is not None:
            result: dict[str, ThresholdCandidate] = {}
            if warn is not None:
                result["warn"] = warn
            if fail is not None:
                result["fail"] = fail
            return result
    return None


def metric_error_bound_decimal(scale: int) -> Decimal:
    return fraction_decimal(Fraction(1, scale))
