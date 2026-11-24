from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

from .codes import E_CHECKPOINT
from .ledger import Ledger
from .util import atomic_write_json


def snapshot_path(workdir: os.PathLike[str] | str) -> Path:
    return Path(workdir) / "snapshots" / "checkpoint-latest.json"


def checkpoint_document_path(workdir: os.PathLike[str] | str) -> Path:
    return Path(workdir) / "checkpoints" / "checkpoint-latest.json"


def find_temp_files(workdir: os.PathLike[str] | str) -> list[str]:
    root = Path(workdir)
    if not root.exists():
        return []
    return sorted(str(path) for path in root.rglob("*.tmp") if path.is_file())


def write_snapshot(checkpoint: dict[str, Any], workdir: os.PathLike[str] | str) -> Path:
    path = snapshot_path(workdir)
    atomic_write_json(path, checkpoint)
    return path


def write_checkpoint_document(
    checkpoint: dict[str, Any], workdir: os.PathLike[str] | str
) -> Path:
    path = checkpoint_document_path(workdir)
    atomic_write_json(path, checkpoint)
    return path


def leave_partial_snapshot(
    checkpoint: dict[str, Any], workdir: os.PathLike[str] | str
) -> Path:
    path = snapshot_path(workdir)
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".partial.tmp")
    partial.write_text(
        '{"sequence": %s, "ledger_digest": "%s", "torn": '
        % (checkpoint.get("sequence"), checkpoint.get("ledger_digest", "")[:12]),
        encoding="utf-8",
    )
    with partial.open("a", encoding="utf-8") as handle:
        handle.flush()
        os.fsync(handle.fileno())
    return partial


def _read_snapshot(path: Path) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    if not path.exists():
        return None, "snapshot file is missing"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"snapshot is unreadable: {exc}"
    if not isinstance(value, dict):
        return None, "snapshot is not a JSON object"
    return value, None


def verify_checkpoint(
    workdir: os.PathLike[str] | str,
    *,
    ledger: Optional[Ledger] = None,
    recover: bool = False,
) -> dict[str, Any]:
    root = Path(workdir)
    root.mkdir(parents=True, exist_ok=True)
    own_ledger = ledger is None
    if own_ledger:
        ledger = Ledger(root / "ledger.sqlite3")

    assert ledger is not None
    try:
        checkpoint = ledger.latest_checkpoint()
        ledger_digest = ledger.current_digest()
        history_errors = ledger.verify_history()
        temp_files = find_temp_files(root)
        snapshot_file = snapshot_path(root)
        checkpoint_file = checkpoint_document_path(root)
        snapshot, snapshot_error = _read_snapshot(snapshot_file)
        checkpoint_document, checkpoint_document_error = _read_snapshot(checkpoint_file)
        recovered_actions: list[str] = []

        document_match = False
        snapshot_match = False
        digest_match = True
        issues: list[str] = []

        if checkpoint is None:
            if snapshot is not None:
                issues.append("snapshot exists but SQLite has no complete checkpoint")
            if checkpoint_document is not None:
                issues.append("checkpoint document exists but SQLite has no checkpoint")
            snapshot_match = snapshot is None
            document_match = checkpoint_document is None
        else:
            digest_match = ledger_digest == checkpoint["ledger_digest"]
            if not digest_match:
                issues.append("ledger digest does not match checkpoint digest")
            if history_errors:
                issues.extend(history_errors)

            if snapshot_error:
                issues.append(snapshot_error)
            elif snapshot is not None:
                snapshot_match = _document_matches(snapshot, checkpoint)
                if not snapshot_match:
                    issues.append("snapshot does not match complete SQLite checkpoint")

            if checkpoint_document_error:
                issues.append(checkpoint_document_error)
            elif checkpoint_document is not None:
                document_match = _document_matches(checkpoint_document, checkpoint)
                if not document_match:
                    issues.append("checkpoint document does not match SQLite checkpoint")

        if temp_files:
            issues.append(f"{len(temp_files)} temporary file(s) remain")

        needs_recovery = bool(temp_files) or (
            checkpoint is not None and (not snapshot_match or not document_match)
        )
        if recover and needs_recovery:
            for temp_file in temp_files:
                try:
                    Path(temp_file).unlink()
                    recovered_actions.append(f"removed temp file {temp_file}")
                except FileNotFoundError:
                    pass
            if checkpoint is not None and not document_match:
                write_checkpoint_document(checkpoint, root)
                recovered_actions.append("rewrote atomic checkpoint document")
            if checkpoint is not None and not snapshot_match:
                write_snapshot(checkpoint, root)
                recovered_actions.append("rewrote snapshot from last complete SQLite checkpoint")
            fresh = verify_checkpoint(root, ledger=ledger, recover=False)
            fresh["recover"] = True
            fresh["recovered_actions"] = recovered_actions + fresh.get(
                "recovered_actions", []
            )
            return fresh

        ok = (
            digest_match
            and not history_errors
            and snapshot_match
            and document_match
            and not temp_files
            and not issues
        )
        return {
            "schema_version": "bsl-checkpoint-verify-v1",
            "ok": ok,
            "code": None if ok else E_CHECKPOINT,
            "workdir": str(root),
            "ledger_digest": ledger_digest,
            "latest_sequence": checkpoint["sequence"] if checkpoint else None,
            "completed_files": sorted(
                (checkpoint or {}).get("completed_files", {}).keys()
            ),
            "checkpoint_document_path": str(checkpoint_file),
            "snapshot_path": str(snapshot_file),
            "checkpoint_document_match": document_match,
            "snapshot_match": snapshot_match,
            "digest_match": digest_match,
            "history_errors": history_errors,
            "temporary_files": temp_files,
            "issues": issues,
            "recover": recover,
            "recovered_actions": recovered_actions,
        }
    finally:
        if own_ledger:
            ledger.close()


def _document_matches(document: dict[str, Any], checkpoint: dict[str, Any]) -> bool:
    return (
        document.get("sequence") == checkpoint["sequence"]
        and document.get("ledger_digest") == checkpoint["ledger_digest"]
        and document.get("completed_files") == checkpoint["completed_files"]
    )
