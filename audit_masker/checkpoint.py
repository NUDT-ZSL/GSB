from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .ledger import LedgerStore
from .util import (
    atomic_write_json,
    digest_ledger_rows,
    sha256_bytes,
)


CHECKPOINT_SCHEMA = "audit-mask-checkpoint/v1"


class CheckpointError(RuntimeError):
    code = "E_CHECKPOINT"


def _watermark(results: List[Dict[str, Any]]) -> Tuple[Optional[str], Optional[int]]:
    if not results:
        return None, None
    latest = max(results, key=lambda item: (item["event_id"], int(item["seq"])))
    return latest["event_id"], int(latest["seq"])


def create_checkpoint(
    store: LedgerStore,
    *,
    snapshot_path: Optional[str] = None,
    inject_fault: bool = False,
) -> Dict[str, Any]:
    ledger_rows = store.load_ledger_rows()
    event_results = store.load_event_results()
    ledger_digest = digest_ledger_rows(ledger_rows)
    revision = store.latest_decision_revision()
    layout_fingerprint = store.meta_get("applied_layout_fingerprint")
    rules_fingerprint = store.meta_get("rules_fingerprint")
    applied_view_state = store.meta_get("applied_view_state")
    defaults_fingerprint = store.meta_get("applied_defaults_fingerprint")
    watermark_event_id, watermark_seq = _watermark(event_results)
    checkpoint_id = f"cp-{revision:06d}-{uuid.uuid4().hex[:12]}"
    created_ms = time.time_ns() // 1_000_000

    if snapshot_path is None:
        digest_prefix = ledger_digest[:12]
        path = (
            store.workspace
            / "checkpoints"
            / f"checkpoint-{revision:06d}-{digest_prefix}.json"
        )
    else:
        path = Path(snapshot_path)
        if not path.is_absolute():
            path = store.workspace / path
    snapshot = {
        "schema": CHECKPOINT_SCHEMA,
        "checkpoint_id": checkpoint_id,
        "created_ms": created_ms,
        "decision_revision": revision,
        "layout_fingerprint": layout_fingerprint,
        "rules_fingerprint": rules_fingerprint,
        "defaults_fingerprint": defaults_fingerprint,
        "ledger_digest": ledger_digest,
        "completed_events": len(event_results),
        "watermark": {
            "event_id": watermark_event_id,
            "seq": watermark_seq,
        },
        "ledger_rows": ledger_rows,
        "event_results": event_results,
        "applied_view_state": applied_view_state,
    }
    snapshot_digest = atomic_write_json(path, snapshot)

    if inject_fault or os.environ.get("AUDIT_MASKER_FAULT") == "after_snapshot":
        os._exit(137)

    row = {
        "checkpoint_id": checkpoint_id,
        "snapshot_path": str(path.resolve()),
        "snapshot_digest": snapshot_digest,
        "ledger_digest": ledger_digest,
        "layout_fingerprint": layout_fingerprint,
        "rules_fingerprint": rules_fingerprint,
        "completed_events": len(event_results),
        "watermark_event_id": watermark_event_id,
        "watermark_seq": watermark_seq,
        "decision_revision": revision,
        "created_ms": created_ms,
        "status": "complete",
    }
    with store.transaction():
        store.insert_checkpoint(row)
    return {
        "checkpoint_id": checkpoint_id,
        "snapshot_path": str(path.resolve()),
        "snapshot_digest": snapshot_digest,
        "ledger_digest": ledger_digest,
        "completed_events": len(event_results),
        "watermark": snapshot["watermark"],
        "decision_revision": revision,
    }


def _validate_snapshot_file(
    row: Dict[str, Any],
) -> Tuple[Optional[Dict[str, Any]], Optional[str], Optional[str]]:
    path = Path(row["snapshot_path"])
    if not path.exists():
        return None, None, "snapshot file is missing"
    try:
        payload = path.read_bytes()
    except OSError as exc:
        return None, None, f"cannot read snapshot: {exc}"
    digest = sha256_bytes(payload)
    if digest != row["snapshot_digest"]:
        return None, digest, "snapshot SHA-256 does not match checkpoint row"
    try:
        snapshot = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, digest, f"snapshot is not parseable JSON: {exc}"
    if not isinstance(snapshot, dict):
        return None, digest, "snapshot root must be an object"
    if snapshot.get("schema") != CHECKPOINT_SCHEMA:
        return None, digest, "snapshot schema is unsupported"
    if snapshot.get("checkpoint_id") != row["checkpoint_id"]:
        return None, digest, "snapshot checkpoint_id does not match row"
    ledger_rows = snapshot.get("ledger_rows")
    if not isinstance(ledger_rows, list):
        return None, digest, "snapshot ledger_rows is missing or invalid"
    computed = digest_ledger_rows(ledger_rows)
    if computed != snapshot.get("ledger_digest"):
        return None, digest, "snapshot ledger digest does not match its rows"
    if computed != row["ledger_digest"]:
        return None, digest, "snapshot ledger digest does not match checkpoint row"
    if snapshot.get("layout_fingerprint") != row["layout_fingerprint"]:
        return None, digest, "snapshot layout fingerprint does not match row"
    if snapshot.get("rules_fingerprint") != row["rules_fingerprint"]:
        return None, digest, "snapshot rules fingerprint does not match row"
    event_results = snapshot.get("event_results")
    if not isinstance(event_results, list):
        return None, digest, "snapshot event_results is missing or invalid"
    if len(event_results) != int(row["completed_events"]):
        return None, digest, "snapshot completed event count does not match row"
    return snapshot, digest, None


def verify_checkpoints(
    store: LedgerStore,
    *,
    current_rules_fingerprint: Optional[str] = None,
) -> Dict[str, Any]:
    rows = store.load_checkpoints()
    current_digest = store.ledger_digest()
    current_layout = store.meta_get("applied_layout_fingerprint")
    valid: List[Dict[str, Any]] = []
    invalid: List[Dict[str, Any]] = []
    for row in rows:
        snapshot, digest, error = _validate_snapshot_file(row)
        if error:
            invalid.append(
                {
                    "checkpoint_id": row["checkpoint_id"],
                    "snapshot_path": row["snapshot_path"],
                    "error": error,
                    "snapshot_digest_actual": digest,
                }
            )
            continue
        valid.append(
            {
                "checkpoint_id": row["checkpoint_id"],
                "snapshot_path": row["snapshot_path"],
                "created_ms": int(row["created_ms"]),
                "ledger_digest": row["ledger_digest"],
                "layout_fingerprint": row["layout_fingerprint"],
                "rules_fingerprint": row["rules_fingerprint"],
                "completed_events": int(row["completed_events"]),
                "watermark": {
                    "event_id": row["watermark_event_id"],
                    "seq": row["watermark_seq"],
                },
                "decision_revision": int(row["decision_revision"]),
                "current_ledger_matches": row["ledger_digest"] == current_digest,
                "current_layout_matches": row["layout_fingerprint"] == current_layout,
                "current_rules_match": (
                    current_rules_fingerprint is None
                    or row["rules_fingerprint"] == current_rules_fingerprint
                ),
                "snapshot": snapshot,
            }
        )

    known_paths = {Path(row["snapshot_path"]).resolve() for row in rows}
    checkpoint_dir = store.workspace / "checkpoints"
    orphans: List[str] = []
    if checkpoint_dir.exists():
        for path in sorted(checkpoint_dir.glob("*.json")):
            if path.resolve() not in known_paths:
                orphans.append(str(path.resolve()))
        for path in sorted(checkpoint_dir.iterdir()):
            if path.is_file() and path.suffix == ".tmp":
                orphans.append(str(path.resolve()))

    latest = valid[-1] if valid else None
    summary = {
        "checkpoint_count": len(rows),
        "valid_count": len(valid),
        "invalid_count": len(invalid),
        "invalid": invalid,
        "orphan_snapshot_count": len(orphans),
        "orphan_snapshots": orphans,
        "current_ledger_digest": current_digest,
        "current_layout_fingerprint": current_layout,
        "current_ledger_matches_latest": (
            latest["ledger_digest"] == current_digest if latest else False
        ),
        "latest_valid": (
            {
                key: value
                for key, value in latest.items()
                if key != "snapshot"
            }
            if latest
            else None
        ),
        "latest_valid_snapshot": latest["snapshot"] if latest else None,
    }
    return summary


def restore_latest_checkpoint(
    store: LedgerStore,
    *,
    current_rules_fingerprint: Optional[str] = None,
) -> Dict[str, Any]:
    verification = verify_checkpoints(
        store,
        current_rules_fingerprint=current_rules_fingerprint,
    )
    latest = verification["latest_valid"]
    snapshot = verification["latest_valid_snapshot"]
    if latest is None or snapshot is None:
        raise CheckpointError("no complete, verifiable checkpoint is available")
    rules_changed = bool(
        current_rules_fingerprint is not None
        and not latest["current_rules_match"]
    )
    if not latest["current_layout_matches"]:
        raise CheckpointError(
            "checkpoint layout does not match the active ledger layout"
        )
    with store.transaction():
        store.replace_checkpoint_state(
            snapshot["ledger_rows"],
            snapshot["event_results"],
            decision_revision=int(snapshot["decision_revision"]),
            applied_view_state=snapshot.get("applied_view_state"),
            applied_layout_fingerprint=snapshot.get("layout_fingerprint"),
            applied_defaults_fingerprint=snapshot.get("defaults_fingerprint"),
            rules_fingerprint=snapshot.get("rules_fingerprint"),
        )
        restored_digest = store.ledger_digest()
        if restored_digest != snapshot["ledger_digest"]:
            raise CheckpointError(
                "restored ledger digest does not match checkpoint after replay"
            )
    return {
        "checkpoint_id": latest["checkpoint_id"],
        "snapshot_path": latest["snapshot_path"],
        "completed_events": latest["completed_events"],
        "watermark": latest["watermark"],
        "ledger_digest": restored_digest,
        "decision_revision": latest["decision_revision"],
        "rules_changed_since_checkpoint": rules_changed,
    }
