from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from .engine import apply_baseline, apply_wal_segment
from .merge import build_merge
from .store import Store, connect_state
from .util import canonical_hash, stable_json
from .validation import effective_clock_ms, validate_v1, validate_v2, validate_wal_manifest


def _latest_recovery(store: Store) -> dict[str, Any] | None:
    row = store.conn.execute(
        "SELECT * FROM recovery_runs ORDER BY sequence_no DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    basis = json.loads(row["basis_json"])
    return {
        "recovery_id": row["recovery_id"],
        "status": row["status"],
        "conclusion": row["conclusion"],
        "clock_ms": row["clock_ms"],
        "ledger_digest": row["ledger_digest"],
        "archive_conclusion": basis.get("archive_conclusion"),
        "item_count": len(basis.get("scan_items", [])),
    }


def build_status(root: Path, clock_ms: int | None = None) -> dict[str, Any]:
    clock = effective_clock_ms(root, clock_ms)
    store = connect_state(root)
    try:
        pointer = store.conn.execute(
            "SELECT * FROM pointer_state WHERE name = 'current_chain'"
        ).fetchone()
        package_pointer = store.conn.execute(
            "SELECT * FROM pointer_state WHERE name = 'current_snapshot_package'"
        ).fetchone()

        table_counts: dict[str, dict[str, int]] = {}
        for table_name in store.logical_tables():
            rows = store.table_rows(table_name)
            table_counts[table_name] = {
                "row_count": len(rows),
                "live_row_count": sum(1 for row in rows if int(row["rv_tombstone"]) == 0),
                "tombstone_count": sum(1 for row in rows if int(row["rv_tombstone"]) == 1),
            }

        ledger: dict[str, Any] | None = None
        ledger_digest: str | None = None
        if pointer is not None:
            ledger_row = store.conn.execute(
                "SELECT ledger_json, ledger_digest FROM ledger_snapshots WHERE view_id = ?",
                (pointer["view_id"],),
            ).fetchone()
            if ledger_row is not None:
                ledger = json.loads(ledger_row["ledger_json"])
                ledger_digest = ledger_row["ledger_digest"]

        segment_rows = [
            dict(row)
            for row in store.conn.execute(
                "SELECT segment_id, generation, version_integer, digest, status "
                "FROM applied_segments ORDER BY segment_id"
            )
        ]
        checkpoint_rows = [
            dict(row)
            for row in store.conn.execute(
                "SELECT generation, checkpoint_id, commit_ms, status, ledger_digest "
                "FROM checkpoints ORDER BY generation"
            )
        ]
        precision_flags = ledger.get("precision_flags", []) if ledger else []
        return {
            "ok": True,
            "command": "status",
            "clock_ms": clock,
            "current_view": dict(pointer) if pointer is not None else None,
            "current_package_pointer": dict(package_pointer) if package_pointer is not None else None,
            "table_counts": dict(sorted(table_counts.items())),
            "ledger_digest": ledger_digest,
            "generation_ledgers": ledger.get("generations", []) if ledger else [],
            "precision_sensitive": [
                flag for flag in precision_flags if bool(flag.get("precision_sensitive"))
            ],
            "precision_flags": precision_flags,
            "clock_gates": ledger.get("clock_gates", []) if ledger else [],
            "generation_boundary": ledger.get("generation_boundary") if ledger else None,
            "checkpoints": checkpoint_rows,
            "segments": segment_rows,
            "recovery": _latest_recovery(store),
        }
    finally:
        store.close()


def cmd_status(root: Path, clock_ms: int | None = None) -> str:
    return stable_json(build_status(root, clock_ms))


def cmd_load(root: Path, clock_ms: int | None = None) -> dict[str, Any]:
    clock = effective_clock_ms(root, clock_ms)
    seed_path = root / "seed_manifest.json"
    from .util import read_json

    seed = read_json(seed_path)
    v1 = validate_v1(root / seed["paths"]["v1"])
    _, v2_entries, _ = validate_v2(root)
    wal_manifest, wal_segments = validate_wal_manifest(root)
    result = {
        "ok": True,
        "command": "load",
        "clock_ms": clock,
        "v1_entries": len(v1["entries"]),
        "v2_entries": len(v2_entries),
        "wal_segments": len(wal_segments),
        "wal_records": sum(int(segment["record_count"]) for segment in wal_segments),
        "wal_set_digest": canonical_hash(wal_manifest["segments"]),
    }
    from .util import write_stable_json

    write_stable_json(root / "artifacts" / "load-validation.json", result)
    return result


def cmd_verify(root: Path, clock_ms: int | None = None) -> dict[str, Any]:
    clock = effective_clock_ms(root, clock_ms)
    merge_one = build_merge(root, clock)
    merge_two = build_merge(root, clock)
    if canonical_hash(merge_one) != canonical_hash(merge_two):
        raise AssertionError("merge construction is not deterministic")
    _, wal_segments = validate_wal_manifest(root)

    store = connect_state(root)
    temp_path: Path | None = None
    temp_store: Store | None = None
    try:
        pointer = store.conn.execute(
            "SELECT * FROM pointer_state WHERE name = 'current_chain'"
        ).fetchone()
        if pointer is None:
            return {
                "ok": False,
                "code": "CHAIN_NOT_MERGED",
                "message": "run merge before verify",
            }
        if pointer["view_id"] != merge_one["view_id"]:
            return {
                "ok": False,
                "code": "CHAIN_VIEW_STALE",
                "message": "stored view does not match the declared clock/sample chain",
                "details": {
                    "stored_view_id": pointer["view_id"],
                    "expected_view_id": merge_one["view_id"],
                },
            }

        ledger_row = store.conn.execute(
            "SELECT ledger_digest, ledger_json FROM ledger_snapshots WHERE view_id = ?",
            (merge_one["view_id"],),
        ).fetchone()
        if ledger_row is None or ledger_row["ledger_digest"] != merge_one["ledger_digest"]:
            return {
                "ok": False,
                "code": "LEDGER_DIGEST_MISMATCH",
                "message": "persisted ledger does not match deterministic recomputation",
            }

        unresolved_segments = [
            dict(row)
            for row in store.conn.execute(
                "SELECT segment_id, status FROM applied_segments WHERE status != 'applied'"
            )
        ]
        unresolved_checkpoints = [
            dict(row)
            for row in store.conn.execute(
                "SELECT generation AS segment_id, status, commit_ms FROM checkpoints "
                "WHERE status NOT IN ('committed', 'waiting_clock')"
            )
        ]
        stale_waiting = [
            dict(row)
            for row in store.conn.execute(
                "SELECT generation AS segment_id, status, commit_ms FROM checkpoints "
                "WHERE status = 'waiting_clock' AND commit_ms <= ?",
                (clock,),
            )
        ]
        unresolved = unresolved_segments + unresolved_checkpoints + stale_waiting
        if unresolved:
            return {
                "ok": False,
                "code": "RECOVERY_INCOMPLETE",
                "message": "half-committed segments or checkpoints remain",
                "details": {"unresolved": unresolved},
            }

        runtime_dir = root / "runtime"
        runtime_dir.mkdir(exist_ok=True)
        descriptor = tempfile.NamedTemporaryFile(
            prefix="verify-", suffix=".sqlite", dir=runtime_dir, delete=False
        )
        descriptor.close()
        temp_path = Path(descriptor.name)
        temp_store = Store(temp_path)
        apply_baseline(temp_store, merge_one, clock)
        for segment in sorted(
            wal_segments, key=lambda item: (int(item["generation"]), str(item["segment_id"]))
        ):
            apply_wal_segment(temp_store, segment, clock)

        mismatches: list[dict[str, Any]] = []
        table_names = sorted(set(store.logical_tables()) | set(temp_store.logical_tables()))
        for table_name in table_names:
            actual = store.table_rows(table_name)
            expected = temp_store.table_rows(table_name)
            if actual != expected:
                mismatches.append(
                    {
                        "table_name": table_name,
                        "actual_rows": len(actual),
                        "expected_rows": len(expected),
                    }
                )
        if mismatches:
            return {
                "ok": False,
                "code": "FULL_REPLAY_MISMATCH",
                "message": "incremental replay differs from empty-database full replay",
                "details": {"tables": mismatches},
            }

        return {
            "ok": True,
            "command": "verify",
            "clock_ms": clock,
            "view_id": merge_one["view_id"],
            "chain_digest": merge_one["chain_digest"],
            "ledger_digest": merge_one["ledger_digest"],
            "tables_verified": table_names,
            "full_replay_equivalence": True,
            "deterministic_merge": True,
        }
    finally:
        if temp_store is not None:
            temp_store.close()
        if temp_path is not None:
            for suffix in ("", "-wal", "-shm"):
                path = Path(str(temp_path) + suffix)
                with __import__("contextlib").suppress(FileNotFoundError):
                    path.unlink()
        store.close()
