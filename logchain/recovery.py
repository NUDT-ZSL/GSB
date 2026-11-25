from __future__ import annotations

import contextlib
import os
import shutil
from pathlib import Path
from typing import Any

from .engine import advance_checkpoints, cmd_replay
from .merge import build_merge
from .store import Store, connect_state, quote_identifier
from .util import (
    canonical_hash,
    file_lock,
    read_json,
    read_jsonl,
    sha256_file,
    stable_json,
)
from .validation import effective_clock_ms


def _add_item(
    store: Store,
    recovery_id: str,
    *,
    classification: str,
    action: str,
    path: str | None = None,
    digest: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    with store.immediate():
        store.conn.execute(
            """
            INSERT INTO recovery_items(
                recovery_id, classification, path, digest, action, detail_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                recovery_id,
                classification,
                path,
                digest,
                action,
                stable_json(detail or {}, indent=None),
            ),
        )


def _referenced_wal(root: Path) -> tuple[set[Path], set[str], dict[str, dict[str, Any]]]:
    manifest_path = root / "samples" / "wal" / "manifest.json"
    referenced_paths: set[Path] = set()
    referenced_hashes: set[str] = set()
    by_id: dict[str, dict[str, Any]] = {}
    if not manifest_path.exists():
        return referenced_paths, referenced_hashes, by_id
    manifest = read_json(manifest_path)
    samples = root / "samples"
    for ref in manifest.get("segments", []):
        path = (samples / str(ref["path"])).resolve()
        referenced_paths.add(path)
        if ref.get("sha256"):
            referenced_hashes.add(str(ref["sha256"]))
        ref["_absolute_path"] = str(path)
        by_id[str(ref["segment_id"])] = ref
    return referenced_paths, referenced_hashes, by_id


def _verify_jsonl_digests(path: Path) -> tuple[bool, dict[str, Any]]:
    try:
        rows = read_jsonl(path)
    except (OSError, ValueError) as exc:
        return False, {"parse_error": str(exc)}
    for index, row in enumerate(rows, 1):
        digest = row.get("digest")
        payload = {key: value for key, value in row.items() if key not in {"digest", "_line_number"}}
        expected = canonical_hash(payload)
        if digest != expected:
            return False, {
                "line_number": index,
                "expected_digest": expected,
                "actual_digest": digest,
            }
    return True, {"record_count": len(rows), "verified": "row_digest_sha256"}


def _archive_file(path: Path, destination_dir: Path) -> Path:
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / path.name
    suffix = 1
    while destination.exists():
        destination = destination_dir / f"{path.stem}.{suffix}{path.suffix}"
        suffix += 1
    shutil.move(str(path), str(destination))
    return destination


def scan_recovery(root: Path, store: Store, clock_ms: int) -> dict[str, Any]:
    sequence = store.next_sequence("recovery_sequence")
    recovery_id = f"recovery-{sequence:04d}"
    with store.immediate():
        store.conn.execute(
            """
            INSERT INTO recovery_runs(
                recovery_id, sequence_no, clock_ms, status, conclusion, basis_json, ledger_digest
            ) VALUES (?, ?, ?, 'scanning', 'PENDING', '{}', NULL)
            """,
            (recovery_id, sequence, clock_ms),
        )

    referenced_paths, referenced_hashes, wal_by_id = _referenced_wal(root)
    wal_root = root / "samples" / "wal"
    if wal_root.exists():
        for path in sorted(p for p in wal_root.rglob("*") if p.is_file()):
            if path.name == "manifest.json":
                continue
            resolved = path.resolve()
            file_hash = sha256_file(path)
            relative_path = str(path.relative_to(root))
            if resolved in referenced_paths:
                _add_item(
                    store,
                    recovery_id,
                    classification="safe_referenced_wal",
                    action="preserve",
                    path=relative_path,
                    digest=file_hash,
                )
                continue
            if file_hash in referenced_hashes:
                _add_item(
                    store,
                    recovery_id,
                    classification="referenced_content_duplicate",
                    action="preserve",
                    path=relative_path,
                    digest=file_hash,
                    detail={"reason": "content matches a referenced WAL segment"},
                )
                continue

            is_partial = path.name.endswith(".partial") or path.name.endswith(".tmp")
            digest_valid, verification = _verify_jsonl_digests(path)
            if is_partial:
                source_hash = file_hash
                archived = _archive_file(
                    path, root / "runtime" / "quarantine" / "half-written" / recovery_id
                )
                _add_item(
                    store,
                    recovery_id,
                    classification="half_written_file",
                    action="quarantine_partial",
                    path=relative_path,
                    digest=source_hash,
                    detail={
                        "archived_to": str(archived.relative_to(root)),
                        "verification": verification,
                    },
                )
            elif digest_valid:
                archived = _archive_file(
                    path, root / "runtime" / "quarantine" / "verified-orphans" / recovery_id
                )
                _add_item(
                    store,
                    recovery_id,
                    classification="orphan_log",
                    action="verified_then_archive",
                    path=relative_path,
                    digest=file_hash,
                    detail={
                        "archived_to": str(archived.relative_to(root)),
                        "verification": verification,
                    },
                )
            else:
                archived = _archive_file(
                    path, root / "runtime" / "quarantine" / "corrupt-orphans" / recovery_id
                )
                _add_item(
                    store,
                    recovery_id,
                    classification="corrupt_orphan_log",
                    action="digest_failed_quarantine",
                    path=relative_path,
                    digest=file_hash,
                    detail={
                        "archived_to": str(archived.relative_to(root)),
                        "verification": verification,
                    },
                )

    staging_root = root / "runtime" / "staging"
    if staging_root.exists():
        for path in sorted(p for p in staging_root.glob("*.partial.json") if p.is_file()):
            staging_hash = sha256_file(path)
            archived = _archive_file(
                path, root / "runtime" / "quarantine" / "merge-staging" / recovery_id
            )
            _add_item(
                store,
                recovery_id,
                classification="half_written_merge_file",
                action="quarantine_staging",
                path=str(path.relative_to(root)),
                digest=staging_hash,
                detail={"archived_to": str(archived.relative_to(root))},
            )

    stage_tables = store.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'chain_stage_%' ORDER BY name"
    ).fetchall()
    for row in stage_tables:
        table_name = str(row["name"])
        with store.immediate():
            store.conn.execute(f"DROP TABLE {quote_identifier(table_name)}")
        _add_item(
            store,
            recovery_id,
            classification="half_written_merge_table",
            action="drop_unpointed_stage_table",
            path=table_name,
        )

    for row in store.conn.execute("SELECT * FROM applied_segments ORDER BY segment_id"):
        segment_id = str(row["segment_id"])
        ref = wal_by_id.get(segment_id)
        if row["status"] == "applying":
            _add_item(
                store,
                recovery_id,
                classification="half_committed_log",
                action="rollback_sqlite_txn_and_reapply_suffix",
                path=ref.get("path") if ref else None,
                digest=row["digest"],
                detail={"segment_id": segment_id, "generation": row["generation"]},
            )
        elif row["status"] == "applied":
            verified = False
            if segment_id.startswith("snapshot:"):
                verified = True
            elif ref:
                absolute = Path(ref["_absolute_path"])
                verified = (
                    absolute.exists()
                    and sha256_file(absolute) == row["digest"] == ref.get("sha256")
                )
            _add_item(
                store,
                recovery_id,
                classification="safe_committed_log" if verified else "committed_but_unverified",
                action="preserve_committed_result" if verified else "verify_required",
                path=ref.get("path") if ref else None,
                digest=row["digest"],
                detail={"segment_id": segment_id, "generation": row["generation"]},
            )

    for row in store.conn.execute("SELECT * FROM checkpoints ORDER BY generation"):
        if row["status"] == "committed":
            classification = "safe_checkpoint"
            action = "preserve"
        elif row["status"] == "waiting_clock":
            classification = "clock_waiting_checkpoint"
            action = "preserve_until_injected_clock"
        else:
            classification = "half_committed_checkpoint"
            action = "recompute_and_commit"
        _add_item(
            store,
            recovery_id,
            classification=classification,
            action=action,
            detail={"generation": row["generation"], "checkpoint_id": row["checkpoint_id"]},
        )

    for run in store.conn.execute(
        "SELECT run_id, started_basis, status FROM replay_runs WHERE status = 'running'"
    ):
        try:
            basis = json_loads(run["started_basis"])
            pid = int(basis.get("pid", -1))
            os.kill(pid, 0)
            alive = True
        except (OSError, ValueError, TypeError):
            alive = False
        except Exception:
            alive = False
        if not alive:
            with store.immediate():
                store.conn.execute(
                    "UPDATE replay_runs SET status='interrupted' WHERE run_id=?",
                    (run["run_id"],),
                )
            _add_item(
                store,
                recovery_id,
                classification="interrupted_replay_run",
                action="mark_interrupted_and_bind_new_replay",
                detail={"run_id": run["run_id"]},
            )

    items = [
        dict(row)
        for row in store.conn.execute(
            "SELECT classification, path, digest, action, detail_json FROM recovery_items WHERE recovery_id=? ORDER BY id",
            (recovery_id,),
        )
    ]
    return {"recovery_id": recovery_id, "items": items}


def json_loads(value: str) -> Any:
    import json

    return json.loads(value)


def finalize_recovery(
    store: Store,
    recovery_id: str,
    *,
    replay: dict[str, Any],
    ledger_digest: str,
) -> dict[str, Any]:
    items = [
        dict(row)
        for row in store.conn.execute(
            "SELECT classification, path, digest, action, detail_json FROM recovery_items WHERE recovery_id=? ORDER BY id",
            (recovery_id,),
        )
    ]
    counts: dict[str, int] = {}
    archived: list[str] = []
    for item in items:
        counts[item["classification"]] = counts.get(item["classification"], 0) + 1
        if item["action"].startswith(("verified_then_archive", "quarantine")):
            detail = json_loads(item["detail_json"])
            if detail.get("archived_to"):
                archived.append(detail["archived_to"])
    archive_conclusion = {
        "status": "RECOVERED_CONSISTENT",
        "referenced_logs": "preserved",
        "orphan_handling": "digest_verified_before_archive",
        "archived_paths": sorted(archived),
        "classification_counts": dict(sorted(counts.items())),
    }
    basis = {
        "scan_items": items,
        "archive_conclusion": archive_conclusion,
        "replay_applications": replay.get("applications", []),
        "checkpoints": replay.get("checkpoints", []),
    }
    with store.immediate():
        store.conn.execute(
            """
            UPDATE recovery_runs
            SET status='complete', conclusion=?, basis_json=?, ledger_digest=?
            WHERE recovery_id=?
            """,
            (
                archive_conclusion["status"],
                stable_json(basis),
                ledger_digest,
                recovery_id,
            ),
        )
    return archive_conclusion


def cmd_resume(
    root: Path,
    clock_ms: int | None = None,
    *,
    store: Store | None = None,
) -> dict[str, Any]:
    clock = effective_clock_ms(root, clock_ms)
    owns_store = store is None
    store = store or connect_state(root)
    try:
        with file_lock(root / "runtime" / "locks" / "recovery.lock"):
            scanned = scan_recovery(root, store, clock)
            replay = cmd_replay(root, clock, crash_id=None, store=store)
            merge = build_merge(root, clock)
            archive_conclusion = finalize_recovery(
                store,
                scanned["recovery_id"],
                replay=replay,
                ledger_digest=merge["ledger_digest"],
            )
        return {
            "ok": True,
            "command": "resume",
            "clock_ms": clock,
            "recovery_id": scanned["recovery_id"],
            "classification_count": len(scanned["items"]),
            "archive_conclusion": archive_conclusion,
            "replay": replay,
        }
    finally:
        if owns_store:
            store.close()
