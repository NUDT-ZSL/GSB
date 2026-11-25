"""Validation for v1 JSON logs and v2 shard/SQLite logs."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from .errors import (
    E_SCHEMA,
    E_V1_CONNECTION_DUPLICATE,
    E_V1_DIRECTION,
    E_V1_RELATIVE_SECOND,
    E_V1_SEQUENCE,
    E_V1_TABLE_MISMATCH,
    E_V2_ABSOLUTE_MS,
    E_V2_GENERATION_ROLLBACK,
    E_V2_SHARD_HASH,
    E_V2_TABLE_VERSION,
    E_V2_TOMBSTONE,
    WorkbenchError,
)
from .jsonio import canonical_hash, read_json, sha256_file
from .packageio import PackageBundle

DIRECTIONS = {"forward", "reverse"}


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def validate_v1(data: dict[str, Any]) -> dict[str, Any]:
    if data.get("format") != "proxy-log-v1":
        raise WorkbenchError(E_SCHEMA, "v1 format marker mismatch", {"format": data.get("format")})
    sessions = data.get("sessions")
    flat_table = data.get("flat_forward_table")
    if not isinstance(sessions, list) or not isinstance(flat_table, list):
        raise WorkbenchError(E_SCHEMA, "v1 sessions and flat_forward_table must be lists")

    flat_by_route: dict[str, dict[str, Any]] = {}
    for entry in flat_table:
        if not isinstance(entry, dict) or not isinstance(entry.get("route_key"), str):
            raise WorkbenchError(E_V1_TABLE_MISMATCH, "flat table entry lacks route_key")
        flat_by_route[entry["route_key"]] = entry

    seen: set[str] = set()
    checked_records = 0
    for session in sessions:
        if not isinstance(session, dict):
            raise WorkbenchError(E_SCHEMA, "v1 session must be an object")
        connection_id = session.get("connection_id")
        if not isinstance(connection_id, str) or not connection_id:
            raise WorkbenchError(E_SCHEMA, "v1 connection_id must be a non-empty string")
        if connection_id in seen:
            raise WorkbenchError(
                E_V1_CONNECTION_DUPLICATE,
                "v1 connection_id must be unique",
                {"connection_id": connection_id},
            )
        seen.add(connection_id)

        records = session.get("records")
        if not isinstance(records, list) or not records:
            raise WorkbenchError(E_SCHEMA, "v1 session records must be a non-empty list", {"connection_id": connection_id})
        previous_seq: int | None = None
        for index, record in enumerate(records):
            if not isinstance(record, dict):
                raise WorkbenchError(E_SCHEMA, "v1 record must be an object", {"connection_id": connection_id, "index": index})
            direction = record.get("direction")
            if direction not in DIRECTIONS:
                raise WorkbenchError(
                    E_V1_DIRECTION,
                    "direction must be forward or reverse",
                    {"connection_id": connection_id, "direction": direction},
                )
            seq = record.get("seq")
            if not _is_int(seq):
                raise WorkbenchError(E_V1_SEQUENCE, "seq must be an integer", {"connection_id": connection_id, "seq": seq})
            if previous_seq is not None and seq <= previous_seq:
                raise WorkbenchError(
                    E_V1_SEQUENCE,
                    "packet seq must be strictly increasing",
                    {"connection_id": connection_id, "previous_seq": previous_seq, "seq": seq},
                )
            previous_seq = seq
            second = record.get("record_second")
            if not _is_int(second) or second < 0:
                raise WorkbenchError(
                    E_V1_RELATIVE_SECOND,
                    "record_second must be a non-negative integer",
                    {"connection_id": connection_id, "value": second},
                )
            checked_records += 1

        refs = session.get("table_refs")
        entries = session.get("forward_table_entries")
        if not isinstance(refs, list) or not isinstance(entries, list):
            raise WorkbenchError(E_V1_TABLE_MISMATCH, "session table_refs and entries are required", {"connection_id": connection_id})
        if not all(isinstance(ref, str) for ref in refs):
            raise WorkbenchError(E_V1_TABLE_MISMATCH, "table_refs must contain strings", {"connection_id": connection_id})
        entries_by_route = {entry.get("route_key"): entry for entry in entries if isinstance(entry, dict)}
        if set(entries_by_route) != set(refs):
            raise WorkbenchError(
                E_V1_TABLE_MISMATCH,
                "session table refs do not match its flat entries",
                {"connection_id": connection_id, "refs": refs, "entry_routes": sorted(entries_by_route)},
            )
        for route_key in refs:
            root_entry = flat_by_route.get(route_key)
            session_entry = entries_by_route.get(route_key)
            if root_entry is None or canonical_hash(root_entry) != canonical_hash(session_entry):
                raise WorkbenchError(
                    E_V1_TABLE_MISMATCH,
                    "session forwarding entry does not match flat table",
                    {"connection_id": connection_id, "route_key": route_key},
                )

    return {"format": "v1", "session_count": len(seen), "record_count": checked_records, "connection_ids": sorted(seen)}


def _validate_metadata_db(db_path: Path) -> dict[str, Any]:
    if not db_path.exists():
        raise WorkbenchError(E_SCHEMA, "v2 metadata SQLite database is missing", {"path": str(db_path)})
    connection = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    try:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        required = {"sessions", "session_generation_history", "tombstones"}
        missing = sorted(required - tables)
        if missing:
            raise WorkbenchError(E_SCHEMA, "v2 metadata is missing required tables", {"missing": missing})

        for connection_id, generation, logical_clock, absolute_ms, table_version in connection.execute(
            "SELECT connection_id, generation, logical_clock_ms, absolute_ms, table_version FROM sessions"
        ):
            if not isinstance(connection_id, str) or not connection_id:
                raise WorkbenchError(E_SCHEMA, "v2 session connection_id invalid")
            if not _is_int(generation) or generation < 1:
                raise WorkbenchError(E_V2_GENERATION_ROLLBACK, "v2 generation must be a positive integer", {"connection_id": connection_id})
            if not _is_int(logical_clock):
                raise WorkbenchError(E_SCHEMA, "v2 logical_clock_ms must be an integer", {"connection_id": connection_id})
            if not _is_int(absolute_ms):
                raise WorkbenchError(E_V2_ABSOLUTE_MS, "v2 absolute_ms must be an integer", {"connection_id": connection_id})
            if not isinstance(table_version, str) or not table_version:
                raise WorkbenchError(E_V2_TABLE_VERSION, "v2 table_version must be a non-empty string", {"connection_id": connection_id})

        rows = connection.execute(
            """
            SELECT connection_id, generation, history_seq, absolute_ms, table_version
            FROM session_generation_history
            ORDER BY connection_id, history_seq
            """
        ).fetchall()
        previous: dict[str, int] = {}
        for connection_id, generation, history_seq, absolute_ms, table_version in rows:
            if not _is_int(generation):
                raise WorkbenchError(E_V2_GENERATION_ROLLBACK, "history generation must be integer", {"connection_id": connection_id})
            if connection_id in previous and generation < previous[connection_id]:
                raise WorkbenchError(
                    E_V2_GENERATION_ROLLBACK,
                    "session generation regressed in SQLite metadata",
                    {
                        "connection_id": connection_id,
                        "previous_generation": previous[connection_id],
                        "generation": generation,
                        "history_seq": history_seq,
                    },
                )
            previous[connection_id] = generation
            if not _is_int(absolute_ms):
                raise WorkbenchError(E_V2_ABSOLUTE_MS, "history absolute_ms must be integer", {"connection_id": connection_id})
            if not isinstance(table_version, str) or not table_version:
                raise WorkbenchError(E_V2_TABLE_VERSION, "history table_version must be string", {"connection_id": connection_id})

        tombstones = connection.execute(
            """
            SELECT tombstone_id, route_key, table_version, observed_absolute_ms, op, is_tombstone, reason
            FROM tombstones
            """
        ).fetchall()
        for tombstone_id, route_key, table_version, observed_ms, op, is_tombstone, reason in tombstones:
            legal = (
                isinstance(tombstone_id, str)
                and bool(tombstone_id)
                and isinstance(route_key, str)
                and bool(route_key)
                and isinstance(table_version, str)
                and bool(table_version)
                and _is_int(observed_ms)
                and op == "tombstone"
                and int(is_tombstone) == 1
                and isinstance(reason, str)
                and bool(reason)
            )
            if not legal:
                raise WorkbenchError(
                    E_V2_TOMBSTONE,
                    "invalid tombstone record",
                    {
                        "tombstone_id": tombstone_id,
                        "route_key": route_key,
                        "table_version": table_version,
                        "observed_absolute_ms": observed_ms,
                        "op": op,
                        "is_tombstone": is_tombstone,
                    },
                )
        return {"session_rows": len(rows), "tombstone_count": len(tombstones)}
    finally:
        connection.close()


def validate_v2(bundle: PackageBundle) -> dict[str, Any]:
    manifest = bundle.v2_manifest
    manifest_path = bundle.v2_manifest_path
    manifest_dir = manifest_path.parent
    if manifest.get("format") != "proxy-log-v2-manifest":
        raise WorkbenchError(E_SCHEMA, "v2 manifest format marker mismatch")
    shards = manifest.get("shards")
    if not isinstance(shards, list) or not shards:
        raise WorkbenchError(E_SCHEMA, "v2 manifest shards must be a non-empty list")

    shard_summaries: list[dict[str, Any]] = []
    session_count = 0
    for shard in shards:
        if not isinstance(shard, dict) or not isinstance(shard.get("path"), str):
            raise WorkbenchError(E_SCHEMA, "v2 shard descriptor invalid", {"shard": shard})
        shard_path = (manifest_dir / shard["path"]).resolve()
        if not shard_path.exists():
            raise WorkbenchError(E_V2_SHARD_HASH, "v2 shard file is missing", {"path": str(shard_path)})
        actual_hash = sha256_file(shard_path)
        expected_hash = shard.get("sha256")
        if actual_hash != expected_hash:
            raise WorkbenchError(
                E_V2_SHARD_HASH,
                "v2 shard sha256 mismatch",
                {"path": shard["path"], "expected": expected_hash, "actual": actual_hash},
            )
        payload = read_json(shard_path)
        if not isinstance(payload, dict) or payload.get("format") != "proxy-log-v2-shard":
            raise WorkbenchError(E_SCHEMA, "v2 shard payload format invalid", {"path": shard["path"]})
        sessions = payload.get("sessions")
        if not isinstance(sessions, list):
            raise WorkbenchError(E_SCHEMA, "v2 shard sessions must be a list", {"path": shard["path"]})
        for session in sessions:
            if not isinstance(session, dict):
                raise WorkbenchError(E_SCHEMA, "v2 session must be object")
            if not _is_int(session.get("generation")):
                raise WorkbenchError(E_V2_GENERATION_ROLLBACK, "v2 session generation must be integer")
            if not isinstance(session.get("table_version"), str):
                raise WorkbenchError(E_V2_TABLE_VERSION, "v2 session table_version must be string")
            for record in session.get("records", []):
                if not isinstance(record, dict) or not _is_int(record.get("absolute_ms")):
                    raise WorkbenchError(E_V2_ABSOLUTE_MS, "v2 record absolute_ms must be integer", {"session": session.get("connection_id")})
        session_count += len(sessions)
        shard_summaries.append({"shard_id": shard.get("shard_id"), "sha256": actual_hash, "session_count": len(sessions)})

    db_name = manifest.get("metadata_db", "metadata.sqlite3")
    metadata = _validate_metadata_db((manifest_dir / str(db_name)).resolve())
    return {
        "format": "v2",
        "package_id": manifest.get("package_id"),
        "manifest_version": manifest.get("manifest_version"),
        "session_count": session_count,
        "shards": shard_summaries,
        "metadata": metadata,
    }


def validate_bundle(bundle: PackageBundle) -> dict[str, Any]:
    report: dict[str, Any] = {"ok": True}
    if bundle.v1 is not None:
        report["v1"] = validate_v1(bundle.v1)
    report["v2"] = validate_v2(bundle)
    return report
