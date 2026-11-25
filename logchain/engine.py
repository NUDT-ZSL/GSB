from __future__ import annotations

import json
import os
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from .crash import CrashController
from .errors import ChainError, ConflictError, ValidationError
from .merge import build_merge
from .store import Store, connect_state, physical_table_name, quote_identifier
from .util import (
    canonical_hash,
    file_lock,
    read_json,
    read_jsonl,
    sha256_file,
    stable_json,
    write_stable_json,
)
from .validation import effective_clock_ms, validate_v1, validate_v2, validate_wal_manifest


CHAIN_TABLE_COLUMNS = """
    chain_order INTEGER PRIMARY KEY,
    entry_id TEXT NOT NULL,
    source_format TEXT NOT NULL,
    generation INTEGER NOT NULL,
    canonical_seq INTEGER NOT NULL,
    txn_seq INTEGER NOT NULL,
    table_name TEXT NOT NULL,
    row_key TEXT NOT NULL,
    op TEXT NOT NULL,
    commit_ms INTEGER NOT NULL,
    entry_json TEXT NOT NULL
"""


def _view_table_name(view_id: str) -> str:
    return "chain_view_" + view_id.replace("view-", "").replace("-", "_")


def _stage_table_name(view_id: str) -> str:
    return "chain_stage_" + view_id.replace("view-", "").replace("-", "_")


def _insert_chain_entries(connection: Any, table: str, entries: list[dict[str, Any]]) -> None:
    quoted = quote_identifier(table)
    connection.execute(f"DELETE FROM {quoted}")
    for entry in entries:
        connection.execute(
            f"""
            INSERT INTO {quoted} (
                chain_order, entry_id, source_format, generation, canonical_seq,
                txn_seq, table_name, row_key, op, commit_ms, entry_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(entry["chain_order"]),
                entry["entry_id"],
                entry["source_format"],
                int(entry["generation"]),
                int(entry["canonical_seq"]),
                int(entry["txn_seq"]),
                entry["table_name"],
                entry["row_key"],
                entry["op"],
                int(entry["commit_ms"]),
                stable_json(entry, indent=None),
            ),
        )


def persist_merge(
    root: Path,
    store: Store,
    merge: dict[str, Any],
    crash: CrashController | None = None,
) -> dict[str, Any]:
    view_id = merge["view_id"]
    final_table = _view_table_name(view_id)
    stage_table = _stage_table_name(view_id)
    lock_path = root / "runtime" / "locks" / "merge.lock"

    with file_lock(lock_path):
        existing = store.conn.execute(
            "SELECT * FROM chain_views WHERE view_id = ?", (view_id,)
        ).fetchone()
        pointer = store.conn.execute(
            "SELECT * FROM pointer_state WHERE name = 'current_chain'"
        ).fetchone()
        if existing and pointer and pointer["view_id"] == view_id:
            _write_merge_artifacts(root, merge)
            return {"decision": "noop_same_view", "view_id": view_id}

        store.conn.execute(f"DROP TABLE IF EXISTS {quote_identifier(stage_table)}")
        store.conn.execute(
            f"CREATE TABLE {quote_identifier(stage_table)} ({CHAIN_TABLE_COLUMNS})"
        )
        _insert_chain_entries(store.conn, stage_table, merge["entries"])

        staging_file = root / "runtime" / "staging" / f"{view_id}.partial.json"
        write_stable_json(staging_file, merge)
        if crash:
            crash.kill_if("merge", "after_stage_write")

        view_sequence = store.next_sequence("chain_view_sequence")
        with store.immediate():
            store.conn.execute(
                f"ALTER TABLE {quote_identifier(stage_table)} RENAME TO {quote_identifier(final_table)}"
            )
            store.conn.execute(
                """
                INSERT OR REPLACE INTO chain_views(
                    view_id, clock_ms, chain_digest, view_table, generation_min,
                    generation_max, status, created_sequence
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    view_id,
                    merge["logical_clock_ms"],
                    merge["chain_digest"],
                    final_table,
                    min(int(item["generation"]) for item in merge["ledger"]["generations"]),
                    max(int(item["generation"]) for item in merge["ledger"]["generations"]),
                    "active",
                    view_sequence,
                ),
            )
            store.conn.execute(
                """
                INSERT OR REPLACE INTO pointer_state(
                    name, view_id, view_table, version_integer, clock_ms, chain_digest
                ) VALUES ('current_chain', ?, ?, ?, ?, ?)
                """,
                (
                    view_id,
                    final_table,
                    1,
                    merge["logical_clock_ms"],
                    merge["chain_digest"],
                ),
            )
            for decision in merge["decisions"]:
                store.conn.execute(
                    """
                    INSERT OR REPLACE INTO merge_decisions(
                        view_id, clock_ms, entry_id, selected_source, winner_key, reason, decision_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        view_id,
                        merge["logical_clock_ms"],
                        decision["entry_id"],
                        decision["selected_source"],
                        decision["winner_key"],
                        decision["reason"],
                        stable_json(decision, indent=None),
                    ),
                )
            store.conn.execute(
                """
                INSERT OR REPLACE INTO ledger_snapshots(
                    view_id, clock_ms, chain_digest, ledger_digest, ledger_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    view_id,
                    merge["logical_clock_ms"],
                    merge["chain_digest"],
                    merge["ledger_digest"],
                    stable_json(merge["ledger"]),
                ),
            )

        with __import__("contextlib").suppress(FileNotFoundError):
            staging_file.unlink()
        _write_merge_artifacts(root, merge)
        return {"decision": "view_switched", "view_id": view_id, "view_table": final_table}


def _write_merge_artifacts(root: Path, merge: dict[str, Any]) -> None:
    artifacts = root / "artifacts"
    write_stable_json(artifacts / "logchain-merge.json", merge)
    write_stable_json(artifacts / "logchain-ledger.json", merge["ledger"])


def cmd_merge(
    root: Path,
    clock_ms: int | None = None,
    crash_id: str | None = None,
) -> dict[str, Any]:
    clock = effective_clock_ms(root, clock_ms)
    crash = CrashController(root, crash_id)
    merge = build_merge(root, clock)
    store = connect_state(root)
    try:
        result = persist_merge(root, store, merge, crash)
        return {"ok": True, "clock_ms": clock, **result, "chain_digest": merge["chain_digest"]}
    finally:
        store.close()


def _apply_entry_sql(
    connection: Any,
    store: Store,
    entry: dict[str, Any],
    *,
    source_segment_id: str,
    clock_ms: int,
) -> None:
    table = str(entry["table_name"])
    physical = physical_table_name(table)
    quoted = quote_identifier(physical)
    row_key = str(entry["row_key"])
    existing = connection.execute(
        f"SELECT rv_version FROM {quoted} WHERE row_key = ?", (row_key,)
    ).fetchone()
    next_version = 1 if existing is None else int(existing["rv_version"]) + 1
    is_delete = entry["op"] == "delete"
    payload = None if is_delete else entry.get("after")
    if not is_delete and payload is None:
        raise ValidationError(
            "WAL_INVALID_OPERATION",
            "non-delete operation requires an after image",
            {"table_name": table, "row_key": row_key},
        )
    connection.execute(
        f"""
        INSERT INTO {quoted} (
            row_key, payload_json, rv_version, rv_generation, rv_tombstone,
            source_segment_id, txn_seq, digest, updated_clock_ms
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(row_key) DO UPDATE SET
            payload_json=excluded.payload_json,
            rv_version=excluded.rv_version,
            rv_generation=excluded.rv_generation,
            rv_tombstone=excluded.rv_tombstone,
            source_segment_id=excluded.source_segment_id,
            txn_seq=excluded.txn_seq,
            digest=excluded.digest,
            updated_clock_ms=excluded.updated_clock_ms
        """,
        (
            row_key,
            stable_json(payload, indent=None),
            next_version,
            int(entry["generation"]),
            1 if is_delete else 0,
            source_segment_id,
            int(entry["txn_seq"]),
            str(entry.get("digest") or canonical_hash(entry)),
            clock_ms,
        ),
    )


def apply_baseline(store: Store, merge: dict[str, Any], clock_ms: int) -> dict[str, Any]:
    view_id = merge["view_id"]
    baseline_id = f"snapshot:{view_id}"
    lock_path = store.path.parent / "locks" / f"baseline-{view_id}.lock"
    with file_lock(lock_path):
        existing = store.conn.execute(
            "SELECT status FROM applied_segments WHERE segment_id = ?", (baseline_id,)
        ).fetchone()
        if existing and existing["status"] == "applied":
            return {"segment_id": baseline_id, "decision": "already_applied"}

        tables = {entry["table_name"] for entry in merge["entries"]}
        for table in tables:
            store.ensure_data_table(str(table))
        basis = stable_json({"type": "merged_snapshot_view", "view_id": view_id}, indent=None)
        with store.immediate():
            store.conn.execute(
                """
                INSERT INTO applied_segments(
                    segment_id, generation, version_integer, digest, status, record_count,
                    clock_ms, applied_clock_ms, basis_json
                ) VALUES (?, 0, 1, ?, 'applying', ?, ?, NULL, ?)
                ON CONFLICT(segment_id) DO UPDATE SET status='applying'
                """,
                (baseline_id, merge["chain_digest"], len(merge["entries"]), clock_ms, basis),
            )
        with store.immediate():
            for entry in merge["entries"]:
                _apply_entry_sql(
                    store.conn, store, entry, source_segment_id=baseline_id, clock_ms=clock_ms
                )
            store.conn.execute(
                "UPDATE applied_segments SET status='applied', applied_clock_ms=? WHERE segment_id=?",
                (clock_ms, baseline_id),
            )
        return {"segment_id": baseline_id, "decision": "applied", "records": len(merge["entries"])}


def apply_wal_segment(
    store: Store,
    segment: dict[str, Any],
    clock_ms: int,
    crash: CrashController | None = None,
) -> dict[str, Any]:
    segment_id = str(segment["segment_id"])
    generation = int(segment["generation"])
    version = int(segment.get("version", 1))
    digest = str(segment["sha256"])
    lock_path = store.path.parent / "locks" / f"{segment_id}.lock"

    with file_lock(lock_path):
        existing = store.conn.execute(
            "SELECT status, digest FROM applied_segments WHERE segment_id = ?", (segment_id,)
        ).fetchone()
        if existing and existing["status"] == "applied":
            if existing["digest"] != digest:
                raise ConflictError(
                    "WAL_SEGMENT_VERSION_CONFLICT",
                    "segment id was already applied with a different digest",
                    {"segment_id": segment_id},
                )
            return {"segment_id": segment_id, "decision": "already_applied"}

        rows = segment.get("_rows")
        if rows is None:
            rows = read_jsonl(Path(segment["_absolute_path"]))
        tables = {row["table_name"] for row in rows}
        for table in tables:
            store.ensure_data_table(str(table))

        basis = stable_json(
            {"path": segment.get("path"), "sha256": digest, "version": version}, indent=None
        )
        with store.immediate():
            store.conn.execute(
                """
                INSERT INTO applied_segments(
                    segment_id, generation, version_integer, digest, status, record_count,
                    clock_ms, applied_clock_ms, basis_json
                ) VALUES (?, ?, ?, ?, 'applying', ?, ?, NULL, ?)
                ON CONFLICT(segment_id) DO UPDATE SET
                    generation=excluded.generation,
                    version_integer=excluded.version_integer,
                    digest=excluded.digest,
                    status='applying',
                    record_count=excluded.record_count,
                    basis_json=excluded.basis_json
                """,
                (segment_id, generation, version, digest, len(rows), clock_ms, basis),
            )

        with store.immediate():
            for index, row in enumerate(rows, 1):
                row_for_entry = {
                    **row,
                    "canonical_seq": int(row["txn_seq"]),
                    "source_format": "wal",
                }
                _apply_entry_sql(
                    store.conn,
                    store,
                    row_for_entry,
                    source_segment_id=segment_id,
                    clock_ms=clock_ms,
                )
                store.conn.execute(
                    """
                    INSERT OR IGNORE INTO applied_mutations(
                        segment_id, generation, txn_seq, table_name, row_key, digest
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        segment_id,
                        generation,
                        int(row["txn_seq"]),
                        row["table_name"],
                        row["row_key"],
                        str(row.get("digest", "")),
                    ),
                )
                if crash:
                    crash.kill_after_record(segment_id=segment_id, applied_count=index)
            store.conn.execute(
                "UPDATE applied_segments SET status='applied', applied_clock_ms=? WHERE segment_id=?",
                (clock_ms, segment_id),
            )
        return {"segment_id": segment_id, "decision": "applied", "records": len(rows)}


def cmd_replay(
    root: Path,
    clock_ms: int | None = None,
    crash_id: str | None = None,
    *,
    store: Store | None = None,
) -> dict[str, Any]:
    clock = effective_clock_ms(root, clock_ms)
    crash = CrashController(root, crash_id)
    merge = build_merge(root, clock)
    owns_store = store is None
    store = store or connect_state(root)
    try:
        pointer = store.conn.execute(
            "SELECT * FROM pointer_state WHERE name = 'current_chain'"
        ).fetchone()
        if pointer is None or pointer["view_id"] != merge["view_id"]:
            persist_merge(root, store, merge)
            pointer = _current_chain_or_error(store)

        run_id = f"replay-{uuid.uuid4().hex}"
        with store.immediate():
            store.conn.execute(
                """
                INSERT INTO replay_runs(run_id, view_id, bound_generation_max, clock_ms, status, started_basis)
                VALUES (?, ?, ?, ?, 'running', ?)
                """,
                (
                    run_id,
                    pointer["view_id"],
                    max(int(item["generation"]) for item in merge["ledger"]["generations"]),
                    clock,
                    stable_json(
                        {"bound_view_id": pointer["view_id"], "pid": os.getpid()},
                        indent=None,
                    ),
                ),
            )

        baseline_result = apply_baseline(store, merge, clock)
        _, segments = validate_wal_manifest(root)
        results = [baseline_result]
        for segment in sorted(segments, key=lambda item: (int(item["generation"]), str(item["segment_id"]))):
            results.append(apply_wal_segment(store, segment, clock, crash))

        with store.immediate():
            store.conn.execute(
                "UPDATE replay_runs SET status='completed' WHERE run_id = ?", (run_id,)
            )
        checkpoint_result = advance_checkpoints(root, store, merge, clock, crash)
        return {
            "ok": True,
            "command": "replay",
            "run_id": run_id,
            "bound_view_id": pointer["view_id"],
            "clock_ms": clock,
            "applications": results,
            "checkpoints": checkpoint_result,
        }
    finally:
        if owns_store:
            store.close()


def advance_checkpoints(
    root: Path,
    store: Store,
    merge: dict[str, Any],
    clock_ms: int,
    crash: CrashController | None = None,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    checkpoints_by_generation: dict[int, dict[str, Any]] = {}
    for checkpoint in merge.get("checkpoints", {}).values():
        checkpoints_by_generation[int(checkpoint["generation"])] = checkpoint

    gates = {
        int(gate["generation"]): gate
        for gate in merge["ledger"].get("clock_gates", [])
    }
    for ledger in merge["ledger"]["generations"]:
        generation = int(ledger["generation"])
        checkpoint = checkpoints_by_generation.get(generation, {})
        checkpoint_id = str(checkpoint.get("checkpoint_id") or f"cp-g{generation}")
        commit_ms = int(merge["generation_commit_ms"][str(generation)])
        gate = gates.get(generation, {})
        if not gate.get("eligible_to_commit", True):
            with store.immediate():
                store.conn.execute(
                    """
                    INSERT INTO checkpoints(
                        generation, checkpoint_id, commit_ms, status, clock_ms, ledger_digest, detail_json
                    ) VALUES (?, ?, ?, 'waiting_clock', ?, ?, ?)
                    ON CONFLICT(generation) DO UPDATE SET
                        status='waiting_clock', clock_ms=excluded.clock_ms, detail_json=excluded.detail_json
                    """,
                    (
                        generation,
                        checkpoint_id,
                        int(gate.get("commit_ms", commit_ms)),
                        clock_ms,
                        merge["ledger_digest"],
                        stable_json(gate, indent=None),
                    ),
                )
            results.append({"generation": generation, "status": "waiting_clock"})
            continue
        with store.immediate():
            store.conn.execute(
                """
                INSERT INTO checkpoints(
                    generation, checkpoint_id, commit_ms, status, clock_ms, ledger_digest, detail_json
                ) VALUES (?, ?, ?, 'committing', ?, ?, ?)
                ON CONFLICT(generation) DO UPDATE SET status='committing', clock_ms=excluded.clock_ms
                """,
                (
                    generation,
                    checkpoint_id,
                    commit_ms,
                    clock_ms,
                    merge["ledger_digest"],
                    stable_json({"phase": "before_final_commit"}, indent=None),
                ),
            )

        if crash:
            crash.kill_if(
                "checkpoint",
                "before_final_commit",
                generation=generation,
            )

        with store.immediate():
            store.conn.execute(
                """
                UPDATE checkpoints
                SET status='committed', ledger_digest=?, detail_json=?
                WHERE generation=?
                """,
                (
                    merge["ledger_digest"],
                    stable_json(
                        {
                            "checkpoint_id": checkpoint_id,
                            "watermark": checkpoint.get("watermark"),
                            "commit_ms": commit_ms,
                            "decision": "committed_under_injected_clock",
                        },
                        indent=None,
                    ),
                    generation,
                ),
            )
            store.conn.execute(
                """
                INSERT OR REPLACE INTO ledger_snapshots(
                    view_id, clock_ms, chain_digest, ledger_digest, ledger_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    merge["view_id"],
                    clock_ms,
                    merge["chain_digest"],
                    merge["ledger_digest"],
                    stable_json(merge["ledger"]),
                ),
            )
        results.append({"generation": generation, "status": "committed"})
    return results


def _current_chain_or_error(store: Store) -> sqlite3.Row:
    pointer = store.conn.execute(
        "SELECT * FROM pointer_state WHERE name = 'current_chain'"
    ).fetchone()
    if pointer is None:
        raise ChainError("CHAIN_NOT_MERGED", "no current log-chain view; run merge first", {})
    return pointer
