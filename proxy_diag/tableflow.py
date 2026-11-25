"""Forward-table patches and dependency invalidation closure."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .errors import E_NOT_FOUND, E_PATCH_INVALID, E_STATE, WorkbenchError
from .jsonio import canonical_hash, read_json
from .store import Store


def validate_patch(patch: dict[str, Any]) -> None:
    if patch.get("format") != "forward-table-patch":
        raise WorkbenchError(E_PATCH_INVALID, "patch format marker mismatch")
    for key in ("patch_id", "base_version", "new_version", "operations", "logical_clock_ms"):
        if key not in patch:
            raise WorkbenchError(E_PATCH_INVALID, f"patch missing {key}")
    if not isinstance(patch["patch_id"], str) or not patch["patch_id"]:
        raise WorkbenchError(E_PATCH_INVALID, "patch_id must be non-empty string")
    if not isinstance(patch["operations"], list) or not patch["operations"]:
        raise WorkbenchError(E_PATCH_INVALID, "patch operations must be a non-empty list")
    for operation in patch["operations"]:
        if operation.get("op") not in {"add", "modify", "upsert", "tombstone"}:
            raise WorkbenchError(E_PATCH_INVALID, "unsupported patch operation", {"op": operation.get("op")})
        if not isinstance(operation.get("key"), str):
            raise WorkbenchError(E_PATCH_INVALID, "operation key must be string")


def _entry_digest(entry: dict[str, Any]) -> str:
    return canonical_hash(
        {k: v for k, v in entry.items() if k not in {"entry_digest", "table_version"}}
    )


def build_new_table(base_entries: list[dict[str, Any]], patch: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    entries = {entry["route_key"]: dict(entry) for entry in base_entries}
    tombstones: list[dict[str, Any]] = []
    new_version = patch["new_version"]
    for operation in patch["operations"]:
        op = operation["op"]
        key = operation["key"]
        if op == "add":
            if key in entries:
                raise WorkbenchError(E_PATCH_INVALID, "cannot add existing route", {"key": key})
            entry = dict(operation.get("entry") or {})
            entry["route_key"] = key
            entry["table_version"] = new_version
            entry["tombstone"] = False
            entry.setdefault("depends_on", [])
            entry["entry_digest"] = _entry_digest(entry)
            entries[key] = entry
        elif op in {"modify", "upsert"}:
            if op == "modify" and key not in entries:
                raise WorkbenchError(E_PATCH_INVALID, "cannot modify missing route", {"key": key})
            old = entries.get(key, {"route_key": key, "depends_on": []})
            if op == "modify" and operation.get("expected_digest") and old.get("entry_digest") != operation["expected_digest"]:
                raise WorkbenchError(
                    E_PATCH_INVALID,
                    "optimistic entry digest does not match base table",
                    {"key": key, "expected": operation["expected_digest"], "actual": old.get("entry_digest")},
                )
            entry = {**old, **dict(operation.get("entry") or {})}
            entry["route_key"] = key
            entry["table_version"] = new_version
            entry["tombstone"] = False
            entry["entry_digest"] = _entry_digest(entry)
            entries[key] = entry
        elif op == "tombstone":
            if key not in entries:
                raise WorkbenchError(E_PATCH_INVALID, "cannot tombstone missing route", {"key": key})
            tombstone = dict(operation.get("tombstone") or {})
            if tombstone.get("route_key") != key or tombstone.get("op") != "tombstone":
                raise WorkbenchError(E_PATCH_INVALID, "invalid tombstone payload", {"key": key})
            entries[key]["tombstone"] = True
            entries[key]["table_version"] = new_version
            entries[key]["entry_digest"] = _entry_digest(entries[key])
            tombstones.append(tombstone)
    return [entries[key] for key in sorted(entries)], tombstones


def dependency_closure(changed_keys: list[str], entries: list[dict[str, Any]]) -> dict[str, Any]:
    reverse_dependencies: dict[str, set[str]] = {}
    for entry in entries:
        for dependency in entry.get("depends_on", []):
            reverse_dependencies.setdefault(dependency, set()).add(entry["route_key"])
    affected: set[str] = set(changed_keys)
    frontier = list(changed_keys)
    while frontier:
        key = frontier.pop()
        for dependent in sorted(reverse_dependencies.get(key, set())):
            if dependent not in affected:
                affected.add(dependent)
                frontier.append(dependent)
    return {
        "changed_keys": sorted(set(changed_keys)),
        "affected_keys": sorted(affected),
        "reverse_dependencies": {key: sorted(value) for key, value in sorted(reverse_dependencies.items())},
    }


def _reusable_evidence(db: sqlite3.Connection, session_ids: list[str]) -> dict[str, dict[str, str]]:
    evidence: dict[str, dict[str, str]] = {}
    for session_id in session_ids:
        row = db.execute(
            "SELECT evidence_hash, result_json FROM forwarding_attempt WHERE connection_id=? AND status='committed' ORDER BY attempt_id DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        if row:
            evidence[session_id] = {
                "evidence_hash": row["evidence_hash"],
                "result_canonical_hash": canonical_hash(json.loads(row["result_json"])),
            }
    return evidence


def apply_patch_file(store: Store, patch_path: str | Path) -> dict[str, Any]:
    path = Path(patch_path)
    if not path.exists():
        raise WorkbenchError(E_NOT_FOUND, "patch file not found", {"path": str(path)})
    patch = read_json(path)
    validate_patch(patch)
    patch_digest = canonical_hash(patch)
    patch_id = patch["patch_id"]

    existing = store.connection.execute("SELECT * FROM applied_patch WHERE patch_id=?", (patch_id,)).fetchone()
    if existing:
        if existing["patch_digest"] != patch_digest:
            raise WorkbenchError(E_PATCH_INVALID, "patch_id reused with different content", {"patch_id": patch_id})
        closure = json.loads(existing["closure_json"])
        closure["idempotent"] = True
        closure["applied"] = False
        return closure

    base_version = patch["base_version"]
    base_row = store.connection.execute("SELECT * FROM table_version WHERE version=?", (base_version,)).fetchone()
    if base_row is None:
        raise WorkbenchError(E_STATE, "base table version is not registered", {"base_version": base_version})
    base_entries = json.loads(base_row["entries_json"])
    new_entries, _tombstones = build_new_table(base_entries, patch)
    changed_keys = [operation["key"] for operation in patch["operations"]]
    closure_keys = dependency_closure(changed_keys, new_entries)
    affected_key_set = set(closure_keys["affected_keys"])

    state_rows = store.connection.execute("SELECT * FROM session_state ORDER BY connection_id").fetchall()
    affected_sessions: list[str] = []
    for row in state_rows:
        refs = set(json.loads(row["route_keys_json"]))
        if refs & affected_key_set:
            affected_sessions.append(row["connection_id"])
    affected_sessions.sort()
    all_sessions = [row["connection_id"] for row in state_rows]
    reusable_sessions = sorted(set(all_sessions) - set(affected_sessions))
    reusable_evidence = _reusable_evidence(store.connection, reusable_sessions)
    clock_ms = int(patch["logical_clock_ms"])

    closure = {
        "ok": True,
        "idempotent": False,
        "applied": True,
        "patch_id": patch_id,
        "patch_digest": patch_digest,
        "from_version": base_version,
        "to_version": patch["new_version"],
        "logical_clock_ms": clock_ms,
        **closure_keys,
        "affected_sessions": affected_sessions,
        "reusable_sessions": reusable_sessions,
        "reusable_evidence_before_patch": reusable_evidence,
    }

    with store.immediate() as db:
        db.execute(
            "INSERT INTO table_version(version, parent_version, digest, entries_json, patch_id, created_clock_ms) VALUES (?, ?, ?, ?, ?, ?)",
            (
                patch["new_version"],
                base_version,
                canonical_hash(new_entries),
                json.dumps(new_entries, sort_keys=True, ensure_ascii=False),
                patch_id,
                clock_ms,
            ),
        )
        db.execute(
            "INSERT INTO closure_record VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                patch_id,
                base_version,
                patch["new_version"],
                json.dumps(closure_keys["affected_keys"], sort_keys=True),
                json.dumps(affected_sessions, sort_keys=True),
                json.dumps(reusable_sessions, sort_keys=True),
                json.dumps(closure, sort_keys=True, ensure_ascii=False),
                clock_ms,
            ),
        )
        db.execute(
            "INSERT INTO applied_patch VALUES (?, ?, ?, ?, ?)",
            (patch_id, patch_digest, patch["new_version"], json.dumps(closure, sort_keys=True, ensure_ascii=False), clock_ms),
        )
        for connection_id in affected_sessions:
            state = db.execute("SELECT * FROM session_state WHERE connection_id=?", (connection_id,)).fetchone()
            dedupe = canonical_hash(
                {
                    "connection_id": connection_id,
                    "snapshot_version": state["snapshot_version"],
                    "generation": state["generation"],
                    "table_version": patch["new_version"],
                }
            )
            db.execute(
                """
                INSERT OR IGNORE INTO forwarding_attempt
                (connection_id, snapshot_version, generation, table_version, dedupe_hash, status, created_clock_ms)
                VALUES (?, ?, ?, ?, ?, 'pending', ?)
                """,
                (connection_id, state["snapshot_version"], state["generation"], patch["new_version"], dedupe, clock_ms),
            )
            db.execute(
                """
                UPDATE session_state
                SET table_version=?, stage='table_lookup', claim_status='pending', claim_worker=NULL,
                    claim_token=NULL, updated_clock_ms=?
                WHERE connection_id=?
                """,
                (patch["new_version"], clock_ms, connection_id),
            )
        store.advance_watermark(clock_ms, db)
    return closure
