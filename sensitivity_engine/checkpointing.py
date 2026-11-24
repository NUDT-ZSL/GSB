"""Checkpoint persistence, verification, stale-file handling, and recovery."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .atomicio import atomic_write_json, read_json, remove_stale_temp_files, stale_temp_files
from .codes import Code
from .domain import SNAPSHOT_FORMAT_VERSION
from .hashing import canonical_json
from .models import Dataset
from .service import prepare_batch
from .store import Store


def validate_checkpoint_payload(payload: Any) -> None:
    if not isinstance(payload, dict):
        raise ValueError("checkpoint payload must be an object")
    required = {
        "payload_type",
        "snapshot_version",
        "layout_version",
        "level_enum_version",
        "watermark_clock",
        "watermark_batch_id",
        "ledger_digest",
        "tables",
    }
    missing = required - set(payload)
    if missing:
        raise ValueError(f"checkpoint missing fields: {sorted(missing)}")


def write_checkpoint(store: Store, snapshot_path: str | Path) -> dict[str, Any]:
    path = Path(snapshot_path)
    previous_version: Any = None
    previous_corrupt = False
    if path.exists():
        try:
            previous_version = read_json(path).get("snapshot_version")
        except Exception:
            previous_corrupt = True

    payload = store.build_checkpoint_payload()
    validate_checkpoint_payload(payload)
    store.save_checkpoint_record(payload)
    written = atomic_write_json(path, payload, validator=validate_checkpoint_payload)
    version_changed = previous_version is not None and previous_version != SNAPSHOT_FORMAT_VERSION
    return {
        "path": str(written),
        "payload": payload,
        "previous_snapshot_version": previous_version,
        "snapshot_version_changed": version_changed,
        "previous_snapshot_corrupt": previous_corrupt,
    }


def verify_checkpoint(store: Store, snapshot_path: str | Path) -> dict[str, Any]:
    path = Path(snapshot_path)
    issues: list[dict[str, Any]] = []
    stale = [str(item) for item in stale_temp_files(path)]
    removed_temp = remove_stale_temp_files(path)

    file_payload: dict[str, Any] | None = None
    if not path.exists():
        issues.append({"code": Code.CHECKPOINT_NOT_FOUND, "message": f"snapshot not found: {path}"})
    else:
        try:
            loaded = read_json(path)
            validate_checkpoint_payload(loaded)
            file_payload = loaded
        except Exception as exc:
            issues.append({"code": Code.CHECKPOINT_CORRUPT, "message": str(exc)})

    db_payload = store.get_checkpoint_record()
    if db_payload is None:
        issues.append({"code": Code.CHECKPOINT_NOT_FOUND, "message": "SQLite checkpoint row is missing"})

    if file_payload is not None and file_payload.get("snapshot_version") != SNAPSHOT_FORMAT_VERSION:
        issues.append(
            {
                "code": Code.SNAPSHOT_VERSION,
                "message": "snapshot format version differs from supported version",
                "found": file_payload.get("snapshot_version"),
                "supported": SNAPSHOT_FORMAT_VERSION,
            }
        )

    if file_payload is not None and db_payload is not None:
        if canonical_json(file_payload) != canonical_json(db_payload):
            issues.append(
                {"code": Code.LEDGER_MISMATCH, "message": "snapshot and SQLite checkpoint payloads differ"}
            )
        elif file_payload.get("ledger_digest") != store.ledger_digest():
            issues.append(
                {
                    "code": Code.LEDGER_MISMATCH,
                    "message": "live SQLite ledger digest does not match complete checkpoint",
                    "checkpoint_digest": file_payload.get("ledger_digest"),
                    "live_digest": store.ledger_digest(),
                }
            )
    elif db_payload is not None and file_payload is None:
        if db_payload.get("ledger_digest") != store.ledger_digest():
            issues.append({"code": Code.LEDGER_MISMATCH, "message": "live ledger differs from SQLite checkpoint"})

    ok = not issues
    return {
        "report_type": "sensitivity-checkpoint-verification",
        "ok": ok,
        "code": Code.OK if ok else issues[0]["code"],
        "snapshot_path": str(path),
        "stale_temp_files": stale,
        "removed_temp_files": removed_temp,
        "issues": issues,
        "watermark": store.watermark(),
        "live_ledger_digest": store.ledger_digest(),
    }


def recover_to_checkpoint(
    store: Store, snapshot_path: str | Path, dataset: Dataset | None = None
) -> dict[str, Any]:
    verification = verify_checkpoint(store, snapshot_path)
    recovery: dict[str, Any] = {
        "report_type": "sensitivity-recovery-report",
        "verification": verification,
        "restored": False,
        "replayed_batches": [],
    }

    payload: dict[str, Any] | None = None
    if verification["ok"]:
        payload = store.get_checkpoint_record()
    else:
        file_payload = None
        try:
            file_payload = read_json(snapshot_path)
            validate_checkpoint_payload(file_payload)
        except Exception:
            file_payload = None
        payload = file_payload or store.get_checkpoint_record()
        if payload is None:
            recovery["status"] = "NO_COMPLETE_CHECKPOINT"
            return recovery
        store.restore_checkpoint_payload(payload)
        recovery["restored"] = True

    assert payload is not None
    wm_clock = int(payload["watermark_clock"])
    wm_batch = str(payload["watermark_batch_id"])

    if dataset is not None:
        pending = [
            prepare_batch(batch)
            for batch in dataset.batches
            if (batch.logical_clock, batch.batch_id) > (wm_clock, wm_batch)
        ]
        pending.sort(key=lambda batch: batch.order_key)
        for batch in pending:
            result = store.apply_batch(batch)
            recovery["replayed_batches"].append(
                {"batch_id": batch.batch_id, "code": result.get("code", Code.OK)}
            )

    write_checkpoint(store, snapshot_path)
    final_verification = verify_checkpoint(store, snapshot_path)
    recovery["final_verification"] = final_verification
    recovery["ok"] = final_verification["ok"]
    recovery["watermark"] = store.watermark()
    recovery["ledger_digest"] = store.ledger_digest()
    return recovery
