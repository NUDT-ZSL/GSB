"""Parser and validator for sharded v2 records plus SQLite metadata."""

from __future__ import annotations

import sqlite3
from fractions import Fraction
from pathlib import Path
from typing import Any

from .canonical import parse_tx_id, require_canonical_tx_id
from .config import Config
from .errors import DiagnosticsError
from .models import PositionRecord
from .util import (
    read_json,
    resolve_path,
    sha256_bytes,
    sha256_file,
    stable_json_bytes,
)

V2_METADATA_COLUMNS = (
    "tx_id",
    "tx_num",
    "commit_time_ms",
    "replay_time_ms",
    "source_node",
    "replica_node",
    "replay_generation",
    "tombstone_applied",
    "logical_clock",
    "payload_bytes",
    "committed",
)


def _fraction_text(value: Fraction) -> str:
    from .canonical import fraction_text

    return fraction_text(value)


def metadata_digest(row: dict[str, Any]) -> str:
    canonical = {key: row[key] for key in V2_METADATA_COLUMNS}
    return sha256_bytes(stable_json_bytes(canonical))


def _validate_manifest(path: Path) -> dict[str, Any]:
    manifest = read_json(path)
    if not isinstance(manifest, dict):
        raise DiagnosticsError("E_MANIFEST", location=str(path), details={"reason": "object required"})

    format_version = manifest.get("format_version")
    if format_version not in (2, "2", "v2"):
        raise DiagnosticsError(
            "E_MANIFEST", location=f"{path}:format_version", details={"value": format_version}
        )
    if not isinstance(manifest.get("package_version"), str) or not manifest["package_version"]:
        raise DiagnosticsError(
            "E_MANIFEST", location=f"{path}:package_version", details={"reason": "non-empty string required"}
        )
    package_type = manifest.get("package_type", "source")
    if package_type not in {"source", "full", "incremental"}:
        raise DiagnosticsError(
            "E_MANIFEST", location=f"{path}:package_type", details={"value": package_type}
        )
    floor = manifest.get("logical_clock_floor")
    if isinstance(floor, bool) or not isinstance(floor, int) or floor < 0:
        raise DiagnosticsError(
            "E_MANIFEST", location=f"{path}:logical_clock_floor", details={"value": repr(floor)}
        )
    tx_min = require_canonical_tx_id(manifest.get("tx_min"), location=f"{path}:tx_min")
    tx_max = require_canonical_tx_id(manifest.get("tx_max"), location=f"{path}:tx_max")
    if tx_min.number > tx_max.number:
        raise DiagnosticsError(
            "E_MANIFEST",
            location=f"{path}:tx_min",
            details={"tx_min": tx_min.canonical, "tx_max": tx_max.canonical},
        )

    shards = manifest.get("shards")
    if not isinstance(shards, list) or not shards:
        raise DiagnosticsError(
            "E_MANIFEST", location=f"{path}:shards", details={"reason": "non-empty array required"}
        )
    metadata_db = manifest.get("metadata_db")
    if not isinstance(metadata_db, dict):
        raise DiagnosticsError(
            "E_MANIFEST", location=f"{path}:metadata_db", details={"reason": "object required"}
        )
    return manifest


def _verify_file_hash(base: Path, entry: dict[str, Any], location: str) -> Path:
    value = entry.get("path")
    expected = entry.get("sha256")
    if not isinstance(value, str) or not value:
        raise DiagnosticsError("E_MANIFEST", location=f"{location}:path", details={"value": value})
    if not isinstance(expected, str) or len(expected) != 64:
        raise DiagnosticsError(
            "E_MANIFEST", location=f"{location}:sha256", details={"value": expected}
        )
    path = resolve_path(base, value)
    actual = sha256_file(path)
    if actual.lower() != expected.lower():
        raise DiagnosticsError(
            "E_V2_HASH",
            location=f"{location}:sha256",
            details={"path": str(path), "expected": expected.lower(), "actual": actual},
        )
    return path


def _read_sqlite_rows(path: Path) -> list[dict[str, Any]]:
    uri = f"{path.as_uri()}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        raise DiagnosticsError(
            "E_V2_SCHEMA", location=str(path), details={"reason": str(exc)}
        ) from exc
    connection.row_factory = sqlite3.Row
    try:
        table_rows = connection.execute("PRAGMA table_info(tx_metadata)").fetchall()
        columns = [row["name"] for row in table_rows]
        if not columns:
            raise DiagnosticsError(
                "E_V2_SCHEMA", location=str(path), details={"reason": "tx_metadata table missing"}
            )
        missing = [column for column in V2_METADATA_COLUMNS if column not in columns]
        if missing:
            raise DiagnosticsError(
                "E_V2_SCHEMA",
                location=str(path),
                details={"missing_columns": missing},
            )
        select = ", ".join(V2_METADATA_COLUMNS)
        rows = connection.execute(f"SELECT {select} FROM tx_metadata ORDER BY tx_num, tx_id").fetchall()
        return [dict(row) for row in rows]
    except sqlite3.Error as exc:
        raise DiagnosticsError(
            "E_V2_SCHEMA", location=str(path), details={"reason": str(exc)}
        ) from exc
    finally:
        connection.close()


def parse_v2(manifest_path: Path, config: Config) -> tuple[list[PositionRecord], dict[str, Any]]:
    manifest_path = manifest_path.resolve()
    manifest = _validate_manifest(manifest_path)
    base = manifest_path.parent

    metadata_entry = manifest["metadata_db"]
    metadata_path = _verify_file_hash(base, metadata_entry, f"{manifest_path}:metadata_db")
    rows = _read_sqlite_rows(metadata_path)
    if not rows:
        raise DiagnosticsError(
            "E_V2_SCHEMA", location=str(metadata_path), details={"reason": "no metadata rows"}
        )

    shard_entries: list[tuple[Path, dict[str, Any]]] = []
    shard_records: dict[str, dict[str, Any]] = {}
    for shard_index, shard_entry in enumerate(manifest["shards"]):
        if not isinstance(shard_entry, dict):
            raise DiagnosticsError(
                "E_MANIFEST",
                location=f"{manifest_path}:shards[{shard_index}]",
                details={"reason": "object required"},
            )
        shard_path = _verify_file_hash(
            base, shard_entry, f"{manifest_path}:shards[{shard_index}]"
        )
        shard = read_json(shard_path)
        if not isinstance(shard, dict) or shard.get("format") != "v2-shard":
            raise DiagnosticsError(
                "E_JSON",
                message="v2 shard must be an object with format=v2-shard",
                location=str(shard_path),
            )
        records = shard.get("records")
        if not isinstance(records, list):
            raise DiagnosticsError(
                "E_JSON",
                message="v2 shard records must be an array",
                location=f"{shard_path}:records",
            )
        shard_entries.append((shard_path, shard_entry))
        for record_index, record in enumerate(records):
            location = f"{shard_path}:records[{record_index}]"
            if not isinstance(record, dict):
                raise DiagnosticsError("E_JSON", location=location)
            tx = require_canonical_tx_id(record.get("tx_id"), location=f"{location}:tx_id")
            if tx.canonical in shard_records:
                raise DiagnosticsError(
                    "E_DUPLICATE_TX",
                    location=f"{location}:tx_id",
                    details={"tx_id": tx.canonical},
                )
            shard_records[tx.canonical] = record

    metadata_by_tx: dict[str, dict[str, Any]] = {}
    for row in rows:
        location = f"{metadata_path}:tx_metadata"
        tx = require_canonical_tx_id(row.get("tx_id"), location=f"{location}:tx_id")
        tx_num = row.get("tx_num")
        if isinstance(tx_num, bool) or not isinstance(tx_num, int) or tx_num != tx.number:
            raise DiagnosticsError(
                "E_TX_ID",
                location=f"{location}:tx_num",
                details={"tx_id": tx.canonical, "tx_num": tx_num},
            )
        if tx.canonical in metadata_by_tx:
            raise DiagnosticsError(
                "E_DUPLICATE_TX",
                location=f"{location}:tx_id",
                details={"tx_id": tx.canonical},
            )
        metadata_by_tx[tx.canonical] = row

    if set(metadata_by_tx) != set(shard_records):
        raise DiagnosticsError(
            "E_V2_METADATA_DIGEST",
            location=str(manifest_path),
            details={
                "missing_from_shards": sorted(set(metadata_by_tx) - set(shard_records))[:10],
                "missing_from_metadata": sorted(set(shard_records) - set(metadata_by_tx))[:10],
            },
        )

    tx_min = require_canonical_tx_id(manifest["tx_min"], location=f"{manifest_path}:tx_min")
    tx_max = require_canonical_tx_id(manifest["tx_max"], location=f"{manifest_path}:tx_max")
    tx_numbers = sorted(metadata_by_tx)
    if int(tx_numbers[0]) != tx_min.number or int(tx_numbers[-1]) != tx_max.number:
        raise DiagnosticsError(
            "E_MANIFEST",
            location=f"{manifest_path}:tx_min",
            details={
                "declared": [tx_min.canonical, tx_max.canonical],
                "actual": [tx_numbers[0], tx_numbers[-1]],
            },
        )

    generation_by_replica: dict[str, int] = {}
    logical_clock_by_replica: dict[str, int] = {}
    records: list[PositionRecord] = []
    clock_floor = int(manifest["logical_clock_floor"])
    for tx_id in tx_numbers:
        row = metadata_by_tx[tx_id]
        location = f"{metadata_path}:tx_metadata[{tx_id}]"
        tx = parse_tx_id(tx_id, location=f"{location}:tx_id")

        def int_column(key: str) -> int:
            value = row.get(key)
            if isinstance(value, bool) or not isinstance(value, int):
                raise DiagnosticsError(
                    "E_V2_SCHEMA",
                    location=f"{location}:{key}",
                    details={"value": repr(value), "reason": "integer required"},
                )
            return value

        commit_time = int_column("commit_time_ms")
        replay_time = int_column("replay_time_ms")
        generation = int_column("replay_generation")
        logical_clock = int_column("logical_clock")
        payload_bytes = int_column("payload_bytes")
        tombstone_raw = int_column("tombstone_applied")
        committed_raw = int_column("committed")

        if commit_time < 0 or replay_time < 0:
            raise DiagnosticsError(
                "E_V2_TIMESTAMP",
                location=location,
                details={"commit_time_ms": commit_time, "replay_time_ms": replay_time},
            )
        if replay_time < commit_time:
            raise DiagnosticsError(
                "E_V2_REPLAY_BEFORE_COMMIT",
                location=location,
                details={"commit_time_ms": commit_time, "replay_time_ms": replay_time},
            )
        if generation < 0:
            raise DiagnosticsError(
                "E_V2_GENERATION", location=f"{location}:replay_generation", details={"value": generation}
            )
        if logical_clock < 0:
            raise DiagnosticsError(
                "E_CLOCK_ROLLBACK", location=f"{location}:logical_clock", details={"value": logical_clock}
            )
        if logical_clock < clock_floor:
            raise DiagnosticsError(
                "E_CLOCK_ROLLBACK",
                location=f"{location}:logical_clock",
                details={"value": logical_clock, "logical_clock_floor": clock_floor},
            )
        if payload_bytes <= 0:
            raise DiagnosticsError(
                "E_V2_BYTES", location=f"{location}:payload_bytes", details={"value": payload_bytes}
            )
        if tombstone_raw not in (0, 1):
            raise DiagnosticsError(
                "E_V2_TOMBSTONE", location=f"{location}:tombstone_applied", details={"value": tombstone_raw}
            )
        if committed_raw not in (0, 1):
            raise DiagnosticsError(
                "E_V2_SCHEMA", location=f"{location}:committed", details={"value": committed_raw}
            )
        tombstone = bool(tombstone_raw)
        committed = bool(committed_raw)
        if tombstone and generation < 1:
            raise DiagnosticsError(
                "E_V2_TOMBSTONE",
                location=f"{location}:tombstone_applied",
                details={"replay_generation": generation, "reason": "applied tombstone requires generation >= 1"},
            )
        source_node = row.get("source_node")
        replica_node = row.get("replica_node")
        if not isinstance(source_node, str) or not source_node:
            raise DiagnosticsError(
                "E_V2_SCHEMA", location=f"{location}:source_node", details={"value": repr(source_node)}
            )
        if not isinstance(replica_node, str) or not replica_node:
            raise DiagnosticsError(
                "E_V2_SCHEMA", location=f"{location}:replica_node", details={"value": repr(replica_node)}
            )

        previous_generation = generation_by_replica.get(replica_node)
        if previous_generation is not None and generation < previous_generation:
            raise DiagnosticsError(
                "E_V2_GENERATION",
                location=f"{location}:replay_generation",
                details={
                    "replica_node": replica_node,
                    "previous": previous_generation,
                    "current": generation,
                },
            )
        generation_by_replica[replica_node] = generation

        previous_clock = logical_clock_by_replica.get(replica_node)
        if previous_clock is not None and logical_clock < previous_clock and config.strict_clock_order:
            raise DiagnosticsError(
                "E_CLOCK_ROLLBACK",
                location=f"{location}:logical_clock",
                details={"replica_node": replica_node, "previous": previous_clock, "current": logical_clock},
            )
        logical_clock_by_replica[replica_node] = logical_clock

        shard_record = shard_records[tx_id]
        shard_payload_bytes = shard_record.get("payload_bytes")
        shard_clock = shard_record.get("logical_clock")
        if (
            isinstance(shard_payload_bytes, bool)
            or not isinstance(shard_payload_bytes, int)
            or shard_payload_bytes <= 0
            or isinstance(shard_clock, bool)
            or not isinstance(shard_clock, int)
            or shard_clock < 0
        ):
            raise DiagnosticsError(
                "E_V2_METADATA_DIGEST",
                location=f"{location}:shard",
                details={
                    "payload_bytes": repr(shard_payload_bytes),
                    "logical_clock": repr(shard_clock),
                },
            )
        if shard_payload_bytes != payload_bytes or shard_clock != logical_clock:
            raise DiagnosticsError(
                "E_V2_METADATA_DIGEST",
                location=f"{location}:shard",
                details={
                    "metadata": {"payload_bytes": payload_bytes, "logical_clock": logical_clock},
                    "shard": {"payload_bytes": shard_payload_bytes, "logical_clock": shard_clock},
                },
            )
        expected_digest = metadata_digest(row)
        if shard_record.get("metadata_digest") != expected_digest:
            raise DiagnosticsError(
                "E_V2_METADATA_DIGEST",
                location=f"{location}:metadata_digest",
                details={"expected": expected_digest, "actual": shard_record.get("metadata_digest")},
            )

        delay = replay_time - commit_time
        original_time = {
            "kind": "absolute_ms",
            "commit_time_ms": commit_time,
            "replay_time_ms": replay_time,
        }
        original_delay = {
            "kind": "v2_absolute_timestamp_difference_ms",
            "commit_time_ms": commit_time,
            "replay_time_ms": replay_time,
            "value": delay,
        }
        record_payload = {
            "tx_id": tx.canonical,
            "source_version": "v2",
            "source_priority": 2,
            "logical_clock": logical_clock,
            "replay_generation": generation,
            "payload_bytes": payload_bytes,
            "committed": committed,
            "commit_time_ms": commit_time,
            "replay_time_ms": replay_time,
            "delay_ms": delay,
            "source_node": source_node,
            "replica_node": replica_node,
            "tombstone_applied": tombstone,
            "metadata_digest": expected_digest,
        }
        records.append(
            PositionRecord(
                tx_id=tx.canonical,
                tx_num=tx.number,
                source_version="v2",
                source_priority=2,
                logical_clock=logical_clock,
                replay_generation=generation,
                source_node=source_node,
                replica_node=replica_node,
                committed=committed,
                payload_bytes=payload_bytes,
                commit_time_ms=commit_time,
                replay_time_ms=replay_time,
                delay_ms=delay,
                tombstone_applied=tombstone,
                original_time=original_time,
                original_delay=original_delay,
                migration=None,
                error_upper_bound=config.observation_error,
                error_sources=(
                    "v2.commit_time_ms: integer-ms observation, +/-0.5 ms resolution",
                    "v2.replay_time_ms: integer-ms observation, +/-0.5 ms resolution",
                    f"v2.observation_error: {_fraction_text(config.observation_error)} ms",
                    f"v2.metadata_digest: {expected_digest}",
                ),
                location=location,
                content_digest=sha256_bytes(stable_json_bytes(record_payload)),
            )
        )

    return records, manifest
