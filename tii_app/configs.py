"""Two-generation configuration snapshots, resolution, and migration."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import EConfigInvalid, ELedgerCorrupt
from .util import atomic_write_json, canonical_json, read_json, sha256_obj


DEFAULT_HYPERPARAMETERS: dict[str, Any] = {
    "seed_offset": 7,
    "weight_scale": 0.8,
    "learning_rate": 0.01,
    "activation": "tanh",
    "rounding": "half_away_from_zero",
    "max_row_block": 64,
    "quantization_version": 1,
    "legacy_momentum": 0.9,
}

SUPPORTED_HYPERPARAMETERS = {
    "seed_offset",
    "weight_scale",
    "learning_rate",
    "activation",
    "rounding",
    "max_row_block",
    "quantization_version",
    "legacy_momentum",
    "tensor_parallelism",
    "notes",
}


@dataclass(frozen=True)
class ResolvedConfig:
    generation: int
    effective_at_ms: int
    hyperparameters: dict[str, Any]
    config_hash: str
    source: str
    shard_indices: tuple[int, ...]
    tombstones: tuple[dict[str, Any], ...] = ()


def _config_hash(generation: int, effective_at_ms: int, hyperparameters: dict[str, Any]) -> str:
    return sha256_obj(
        {
            "generation": generation,
            "effective_at_ms": effective_at_ms,
            "hyperparameters": hyperparameters,
        }
    )


def _v1_checksum(document: dict[str, Any]) -> str:
    payload = {key: value for key, value in document.items() if key != "checksum"}
    return sha256_obj(payload)


def _v2_shard_checksum(document: dict[str, Any]) -> str:
    payload = {key: value for key, value in document.items() if key != "checksum"}
    return sha256_obj(payload)


def normalize_v1_document(
    document: dict[str, Any],
    *,
    fill_defaults: bool = True,
) -> tuple[dict[str, Any], dict[str, str], list[dict[str, Any]]]:
    """Normalize a v1 document and report provenance for every field."""

    if not isinstance(document, dict):
        raise EConfigInvalid("v1 config must be a JSON object")
    source: dict[str, str] = {}
    filled: list[dict[str, Any]] = []
    generation = int(document.get("config_version", document.get("generation", 1)))
    source["config_version"] = (
        "v1:config_version" if "config_version" in document else "default:config_version=1"
    )
    if "config_version" not in document:
        filled.append({"field": "config_version", "value": 1, "source": "default"})

    relative_seconds = float(document.get("effective_relative_seconds", 0.0))
    source["effective_relative_seconds"] = (
        "v1:effective_relative_seconds"
        if "effective_relative_seconds" in document
        else "default:effective_relative_seconds=0.0"
    )
    if "effective_relative_seconds" not in document:
        filled.append(
            {"field": "effective_relative_seconds", "value": 0.0, "source": "default"}
        )

    raw_hyperparameters = document.get("hyperparameters", {})
    if not isinstance(raw_hyperparameters, dict):
        raise EConfigInvalid("v1 hyperparameters must be an object")
    hyperparameters = dict(raw_hyperparameters)
    for field, default in DEFAULT_HYPERPARAMETERS.items():
        path = f"hyperparameters.{field}"
        if field in hyperparameters:
            source[path] = f"v1:hyperparameters.{field}"
        elif fill_defaults:
            hyperparameters[field] = default
            source[path] = f"default:{canonical_json(default)}"
            filled.append({"field": path, "value": default, "source": "default"})
        else:
            source[path] = "missing"
    for field in hyperparameters:
        source.setdefault(f"hyperparameters.{field}", f"v1:hyperparameters.{field}")

    compatibility = int(document.get("compatibility_version", 1))
    source["compatibility_version"] = (
        "v1:compatibility_version"
        if "compatibility_version" in document
        else "default:compatibility_version=1"
    )
    normalized = {
        "format_version": 1,
        "config_version": generation,
        "effective_relative_seconds": relative_seconds,
        "hyperparameters": hyperparameters,
        "compatibility_version": compatibility,
    }
    normalized["checksum"] = _v1_checksum(normalized)
    return normalized, source, filled


def read_v1_document(path: str | Path, *, verify: bool = True) -> dict[str, Any]:
    document = read_json(path)
    if (
        verify
        and "checksum" in document
        and document.get("checksum") != _v1_checksum(document)
    ):
        raise ELedgerCorrupt(
            "v1 config checksum mismatch",
            {"path": str(path), "declared": document.get("checksum")},
        )
    return document


def write_v1_document(path: str | Path, document: dict[str, Any]) -> None:
    document = {key: value for key, value in document.items() if key != "checksum"}
    document["format_version"] = 1
    document["checksum"] = _v1_checksum(document)
    atomic_write_json(path, document)


def _read_v2_shard(path: Path, expected_checksum: str | None = None) -> dict[str, Any]:
    if not path.exists():
        raise EConfigInvalid("v2 config shard not found", {"path": str(path)})
    document = read_json(path)
    checksum = _v2_shard_checksum(document)
    if document.get("checksum") != checksum:
        raise ELedgerCorrupt(
            "v2 shard checksum mismatch",
            {"path": str(path), "declared": document.get("checksum"), "computed": checksum},
        )
    if expected_checksum is not None and checksum != expected_checksum:
        raise ELedgerCorrupt(
            "v2 shard does not match SQLite metadata checksum",
            {"path": str(path), "metadata": expected_checksum, "computed": checksum},
        )
    return document


def load_v2_snapshot(config_dir: str | Path) -> dict[str, Any]:
    """Load all v2 generations, active shards, and tombstones.

    The SQLite metadata database is authoritative for absolute time,
    generation, shard path, summary, and tombstone records.
    """

    config_dir = Path(config_dir)
    db_path = config_dir / "meta.sqlite"
    if not db_path.exists():
        raise EConfigInvalid("v2 SQLite metadata database not found", {"path": str(db_path)})
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        shard_rows = connection.execute(
            """
            SELECT config_generation, effective_at_ms, shard_index, shard_path,
                   hyperparameter_summary_json, checksum, is_tombstone
            FROM config_shards
            ORDER BY config_generation, effective_at_ms, shard_index
            """
        ).fetchall()
        tombstone_rows = connection.execute(
            """
            SELECT tombstone_id, config_generation, field_path, effective_at_ms, reason
            FROM config_tombstones
            ORDER BY config_generation, field_path, tombstone_id
            """
        ).fetchall()
    except sqlite3.DatabaseError as exc:
        raise EConfigInvalid("unable to read v2 SQLite metadata", {"path": str(db_path)}) from exc
    finally:
        connection.close()

    generations: dict[int, dict[str, Any]] = {}
    for (
        generation,
        effective_at_ms,
        shard_index,
        shard_path,
        summary_json,
        checksum,
        is_tombstone,
    ) in shard_rows:
        if int(is_tombstone):
            continue
        document = _read_v2_shard(config_dir / shard_path, expected_checksum=checksum)
        if int(document["config_generation"]) != int(generation):
            raise EConfigInvalid(
                "shard generation differs from SQLite metadata",
                {"path": shard_path},
            )
        if int(document["effective_at_ms"]) != int(effective_at_ms):
            raise EConfigInvalid(
                "shard effective time differs from SQLite metadata",
                {"path": shard_path},
            )
        parsed_summary = json.loads(summary_json)
        if not isinstance(parsed_summary, dict):
            raise EConfigInvalid("shard summary must be an object", {"path": shard_path})
        shard_hyperparameters = document.get("hyperparameters", {})
        if not isinstance(shard_hyperparameters, dict):
            raise EConfigInvalid("shard hyperparameters must be an object", {"path": shard_path})
        entry = generations.setdefault(
            int(generation),
            {
                "config_generation": int(generation),
                "effective_at_ms": int(effective_at_ms),
                "hyperparameters": {},
                "shards": [],
                "tombstones": [],
            },
        )
        entry["hyperparameters"].update(shard_hyperparameters)
        entry["shards"].append(
            {
                "shard_index": int(shard_index),
                "shard_path": shard_path,
                "checksum": checksum,
                "summary": parsed_summary,
            }
        )

    for tombstone_id, generation, field_path, effective_at_ms, reason in tombstone_rows:
        entry = generations.get(int(generation))
        if entry is None:
            entry = generations.setdefault(
                int(generation),
                {
                    "config_generation": int(generation),
                    "effective_at_ms": int(effective_at_ms),
                    "hyperparameters": {},
                    "shards": [],
                    "tombstones": [],
                },
            )
        entry["tombstones"].append(
            {
                "tombstone_id": str(tombstone_id),
                "config_generation": int(generation),
                "field_path": str(field_path),
                "effective_at_ms": int(effective_at_ms),
                "reason": str(reason),
            }
        )

    for entry in generations.values():
        entry["hyperparameters"] = dict(sorted(entry["hyperparameters"].items()))
        entry["shards"] = sorted(entry["shards"], key=lambda item: item["shard_index"])
        entry["tombstones"] = sorted(entry["tombstones"], key=lambda item: item["field_path"])
        entry["config_hash"] = _config_hash(
            entry["config_generation"],
            entry["effective_at_ms"],
            entry["hyperparameters"],
        )
    return {"generations": sorted(generations.values(), key=lambda item: item["config_generation"])}


def _to_resolved_v2(entry: dict[str, Any]) -> ResolvedConfig:
    return ResolvedConfig(
        generation=int(entry["config_generation"]),
        effective_at_ms=int(entry["effective_at_ms"]),
        hyperparameters=dict(entry["hyperparameters"]),
        config_hash=str(entry["config_hash"]),
        source="v2",
        shard_indices=tuple(int(item["shard_index"]) for item in entry["shards"]),
        tombstones=tuple(entry.get("tombstones", [])),
    )


def resolved_v1(document: dict[str, Any]) -> ResolvedConfig:
    normalized, _, _ = normalize_v1_document(document)
    effective_at_ms = int(round(float(normalized["effective_relative_seconds"]) * 1000.0))
    return ResolvedConfig(
        generation=int(normalized["config_version"]),
        effective_at_ms=effective_at_ms,
        hyperparameters=dict(normalized["hyperparameters"]),
        config_hash=_config_hash(
            int(normalized["config_version"]),
            effective_at_ms,
            normalized["hyperparameters"],
        ),
        source="v1",
        shard_indices=(),
    )


class ConfigStore:
    def __init__(
        self,
        v1_path: str | Path,
        v2_dir: str | Path,
    ) -> None:
        self.v1_path = Path(v1_path)
        self.v2_dir = Path(v2_dir)
        self.v1_document = read_v1_document(self.v1_path)
        self.v1_resolved = resolved_v1(self.v1_document)
        self.v2_snapshot = load_v2_snapshot(self.v2_dir)

    def snapshot_identity(self) -> str:
        return sha256_obj(
            {
                "v1_checksum": self.v1_document.get("checksum"),
                "v2_generations": [
                    {
                        "generation": item["config_generation"],
                        "effective_at_ms": item["effective_at_ms"],
                        "config_hash": item["config_hash"],
                        "shard_checksums": [shard["checksum"] for shard in item["shards"]],
                        "tombstones": item["tombstones"],
                    }
                    for item in self.v2_snapshot["generations"]
                ],
            }
        )

    def config_snapshot(self) -> dict[str, Any]:
        return {
            "v1": self.v1_document,
            "v2": self.v2_snapshot,
            "source_identity": self.snapshot_identity(),
        }

    def resolve_at(self, logical_clock_ms: int) -> ResolvedConfig:
        candidates: list[ResolvedConfig] = []
        if self.v1_resolved.effective_at_ms <= logical_clock_ms:
            candidates.append(self.v1_resolved)
        for entry in self.v2_snapshot["generations"]:
            if int(entry["effective_at_ms"]) <= logical_clock_ms:
                candidates.append(_to_resolved_v2(entry))
        if not candidates:
            raise EConfigInvalid(
                "no configuration effective at logical time",
                {"logical_clock_ms": logical_clock_ms},
            )
        return max(candidates, key=lambda item: (item.generation, item.effective_at_ms))

    def v2_generations(self) -> list[ResolvedConfig]:
        return [_to_resolved_v2(entry) for entry in self.v2_snapshot["generations"]]

    def active_configuration_at_end(self) -> ResolvedConfig:
        tasks_like_clock = max(
            [self.v1_resolved.effective_at_ms]
            + [int(item["effective_at_ms"]) for item in self.v2_snapshot["generations"]]
        )
        return self.resolve_at(tasks_like_clock)


def _write_v2_shard(path: Path, document: dict[str, Any]) -> str:
    document = {key: value for key, value in document.items() if key != "checksum"}
    document["checksum"] = _v2_shard_checksum(document)
    atomic_write_json(path, document)
    return document["checksum"]


def write_v2_snapshot(
    output_dir: str | Path,
    *,
    generations: list[dict[str, Any]],
    tombstones: list[dict[str, Any]],
) -> None:
    """Write v2 shards and SQLite metadata.

    Each generation has one or more shard objects with:
    format_version, config_generation, effective_at_ms, shard_index,
    hyperparameters, and checksum.
    """

    output_dir = Path(output_dir)
    shard_dir = output_dir / "shards"
    shard_dir.mkdir(parents=True, exist_ok=True)
    db_path = output_dir / "meta.sqlite"
    if db_path.exists():
        db_path.unlink()
    connection = sqlite3.connect(db_path)
    try:
        connection.executescript(
            """
            CREATE TABLE config_shards (
                config_generation INTEGER NOT NULL,
                effective_at_ms INTEGER NOT NULL,
                shard_index INTEGER NOT NULL,
                shard_path TEXT NOT NULL,
                hyperparameter_summary_json TEXT NOT NULL,
                checksum TEXT NOT NULL,
                is_tombstone INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (config_generation, shard_index)
            );
            CREATE TABLE config_tombstones (
                tombstone_id TEXT PRIMARY KEY,
                config_generation INTEGER NOT NULL,
                field_path TEXT NOT NULL,
                effective_at_ms INTEGER NOT NULL,
                reason TEXT NOT NULL
            );
            """
        )
        for generation in generations:
            generation_number = int(generation["config_generation"])
            effective_at_ms = int(generation["effective_at_ms"])
            shard_indices: list[int] = []
            for shard_index, shard in enumerate(generation["shards"]):
                shard_indices.append(shard_index)
                path = shard_dir / f"gen-{generation_number}-shard-{shard_index:04d}.json"
                document = {
                    "format_version": 2,
                    "config_generation": generation_number,
                    "effective_at_ms": effective_at_ms,
                    "shard_index": shard_index,
                    "hyperparameters": shard["hyperparameters"],
                }
                checksum = _write_v2_shard(path, document)
                summary = {
                    "field_count": len(shard["hyperparameters"]),
                    "fields": sorted(shard["hyperparameters"]),
                    "hyperparameter_hash": sha256_obj(shard["hyperparameters"]),
                }
                connection.execute(
                    """
                    INSERT INTO config_shards
                    (config_generation, effective_at_ms, shard_index, shard_path,
                     hyperparameter_summary_json, checksum, is_tombstone)
                    VALUES (?, ?, ?, ?, ?, ?, 0)
                    """,
                    (
                        generation_number,
                        effective_at_ms,
                        shard_index,
                        str(path.relative_to(output_dir)),
                        canonical_json(summary),
                        checksum,
                    ),
                )
            if not shard_indices:
                raise EConfigInvalid(
                    "v2 generation must contain at least one shard",
                    {"generation": generation_number},
                )
        for tombstone in tombstones:
            connection.execute(
                """
                INSERT INTO config_tombstones
                (tombstone_id, config_generation, field_path, effective_at_ms, reason)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    tombstone["tombstone_id"],
                    int(tombstone["config_generation"]),
                    tombstone["field_path"],
                    int(tombstone["effective_at_ms"]),
                    tombstone["reason"],
                ),
            )
        connection.commit()
    finally:
        connection.close()


def migrate_v1_to_v2(
    document: dict[str, Any],
    *,
    base_epoch_ms: int = 0,
    target_generation: int | None = None,
) -> dict[str, Any]:
    normalized, source, filled = normalize_v1_document(document)
    generation = (
        int(target_generation)
        if target_generation is not None
        else int(normalized["config_version"]) + 1
    )
    effective_at_ms = base_epoch_ms + int(
        round(float(normalized["effective_relative_seconds"]) * 1000.0)
    )
    source["config_generation"] = f"computed:v1.config_version+1={generation}"
    source["effective_at_ms"] = (
        f"computed:v1.effective_relative_seconds*1000+base_epoch_ms={effective_at_ms}"
    )
    source["shard_index"] = "derived:single-shard-index=0"
    source["tombstones"] = "default:empty"
    hyperparameters = normalized["hyperparameters"]
    for field in hyperparameters:
        source.setdefault(f"hyperparameters.{field}", f"v1:hyperparameters.{field}")
    return {
        "migration": "v1_to_v2",
        "config_generation": generation,
        "effective_at_ms": effective_at_ms,
        "shards": [{"hyperparameters": hyperparameters, "shard_index": 0}],
        "tombstones": [],
        "field_sources": source,
        "filled_defaults": filled,
        "dropped_fields": [],
    }


def migrate_v2_to_v1(
    config_dir: str | Path,
    *,
    base_epoch_ms: int = 0,
) -> dict[str, Any]:
    snapshot = load_v2_snapshot(config_dir)
    generations = snapshot["generations"]
    if not generations:
        raise EConfigInvalid("v2 snapshot has no configuration generation")
    latest = max(generations, key=lambda item: item["config_generation"])
    merged_hyperparameters: dict[str, Any] = {}
    source: dict[str, str] = {}
    filled: list[dict[str, Any]] = []
    for shard in latest["shards"]:
        path = Path(config_dir) / shard["shard_path"]
        document = _read_v2_shard(path, expected_checksum=shard["checksum"])
        for field, value in document["hyperparameters"].items():
            merged_hyperparameters[field] = value
            source[f"hyperparameters.{field}"] = (
                f"v2:generation={latest['config_generation']}:shard={shard['shard_index']}"
            )
    for field, default in DEFAULT_HYPERPARAMETERS.items():
        if field not in merged_hyperparameters:
            merged_hyperparameters[field] = default
            source[f"hyperparameters.{field}"] = f"default:{canonical_json(default)}"
            filled.append(
                {
                    "field": f"hyperparameters.{field}",
                    "value": default,
                    "source": "default",
                }
            )

    relative_seconds = max(
        0.0,
        (int(latest["effective_at_ms"]) - int(base_epoch_ms)) / 1000.0,
    )
    source["config_version"] = "computed:v1-compatible-generation=1"
    source["effective_relative_seconds"] = (
        f"computed:(v2.effective_at_ms-base_epoch_ms)/1000={relative_seconds}"
    )
    v1_document = {
        "format_version": 1,
        "config_version": 1,
        "effective_relative_seconds": relative_seconds,
        "hyperparameters": merged_hyperparameters,
        "compatibility_version": 1,
    }
    v1_document["checksum"] = _v1_checksum(v1_document)
    dropped: list[dict[str, Any]] = [
        {
            "field": "config_generation",
            "value": latest["config_generation"],
            "rule": "v1 has no generation axis; compatibility generation is set to 1",
        },
        {
            "field": "shard_indices",
            "value": [item["shard_index"] for item in latest["shards"]],
            "rule": "v1 is a single-file unsharded snapshot",
        },
    ]
    for tombstone in latest["tombstones"]:
        dropped.append(
            {
                "field": f"tombstone:{tombstone['field_path']}",
                "value": tombstone,
                "rule": "v1 has no tombstone representation",
            }
        )
    return {
        "migration": "v2_to_v1",
        "v1": v1_document,
        "field_sources": source,
        "filled_defaults": filled,
        "dropped_fields": dropped,
    }
