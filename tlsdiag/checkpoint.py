"""Atomic checkpoint persistence and last-complete-checkpoint recovery."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import codes
from .budget import LockConflict
from .locks import atomic_write_many_locked, cleanup_stale_temp_files, file_lock
from .models import (
    ConnectionResult,
    Diagnostic,
    TrustView,
    canonical_json,
    digest_value,
    sha256_hex,
)


def _checkpoint_payload(
    *,
    input_fingerprint: str,
    config_fingerprint: str,
    trust_view: TrustView,
    completed: Dict[str, ConnectionResult],
    sequence: int,
    previous_sequence: int = 0,
) -> Dict[str, Any]:
    ordered_ids = sorted(completed)
    entries = {
        conn_id: {
            "digest": completed[conn_id].conclusion_digest,
            "result": completed[conn_id].to_dict(),
        }
        for conn_id in ordered_ids
    }
    aggregate_digest = digest_value(
        [(conn_id, completed[conn_id].conclusion_digest) for conn_id in ordered_ids]
    )
    payload: Dict[str, Any] = {
        "format": "tlsdiag-checkpoint",
        "structure_version": codes.CHECKPOINT_STRUCTURE_VERSION,
        "engine_version": codes.ENGINE_VERSION,
        "merge_policy_version": codes.TRUST_MERGE_POLICY_VERSION,
        "sequence_policy_version": codes.SEQUENCE_POLICY_VERSION,
        "input_fingerprint": input_fingerprint,
        "config_fingerprint": config_fingerprint,
        "trust_view_version": trust_view.version,
        "trust_summary": trust_view.trusted_summary(),
        "completed_count": len(ordered_ids),
        "ordered_connection_ids": ordered_ids,
        "aggregate_digest": aggregate_digest,
        "completed": entries,
        "sequence": sequence,
        "previous_sequence": previous_sequence,
    }
    payload["payload_sha256"] = sha256_hex(canonical_json(payload))
    return payload


def save_checkpoint(
    path: str | Path,
    *,
    input_fingerprint: str,
    config_fingerprint: str,
    trust_view: TrustView,
    completed: Dict[str, ConnectionResult],
    sequence: int,
    lock_timeout_ms: int = 0,
) -> Dict[str, Any]:
    target = Path(path)
    previous_path = target.with_name(target.stem + ".prev" + target.suffix)
    lock_path = target.parent / ".tlsdiag.lock"
    target.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(lock_path, timeout_ms=lock_timeout_ms, purpose="checkpoint"):
        previous_sequence = 0
        backups: Dict[str, bytes] = {}
        if target.exists():
            old = _read_checkpoint_file(target)
            if old is not None:
                previous_sequence = int(old.get("sequence", 0))
                try:
                    backups[str(previous_path)] = target.read_bytes()
                except OSError:
                    pass
        payload = _checkpoint_payload(
            input_fingerprint=input_fingerprint,
            config_fingerprint=config_fingerprint,
            trust_view=trust_view,
            completed=completed,
            sequence=sequence,
            previous_sequence=previous_sequence,
        )
        data = (
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        items = dict(backups)
        items[str(target)] = data
        atomic_write_many_locked(items)
    return payload


def _validate_checkpoint_dict(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict):
        return "checkpoint root must be an object"
    checksum = payload.get("payload_sha256")
    if not isinstance(checksum, str):
        return "checkpoint has no payload checksum"
    body = dict(payload)
    body.pop("payload_sha256", None)
    actual = sha256_hex(canonical_json(body))
    if actual != checksum:
        return "checkpoint payload checksum mismatch"
    if not isinstance(payload.get("completed"), dict):
        return "checkpoint completed section is missing or invalid"
    if not isinstance(payload.get("ordered_connection_ids"), list):
        return "checkpoint ordered_connection_ids is missing or invalid"
    return None


def _read_checkpoint_file(path: Path) -> Optional[Dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if _validate_checkpoint_dict(payload) is not None:
        return None
    return payload


@dataclass
class CheckpointLoad:
    usable: bool = False
    rebuild: bool = True
    reasons: List[str] = field(default_factory=list)
    diagnostics: List[Diagnostic] = field(default_factory=list)
    completed: Dict[str, ConnectionResult] = field(default_factory=dict)
    recovered_from: Optional[str] = None
    half_write_detected: bool = False
    sequence: int = 0
    previous_trust_summary: List[Dict[str, Any]] = field(default_factory=list)
    previous_trust_version: Optional[str] = None
    cleaned_temp_files: List[str] = field(default_factory=list)
    aggregate_digest: Optional[str] = None


def load_checkpoint(
    path: str | Path,
    *,
    expected_input_fingerprint: str,
    expected_config_fingerprint: str,
    expected_trust_version: str,
    merge_policy_version: str = codes.TRUST_MERGE_POLICY_VERSION,
) -> CheckpointLoad:
    target = Path(path)
    result = CheckpointLoad()
    lock_path = target.parent / ".tlsdiag.lock"
    try:
        with file_lock(lock_path, timeout_ms=0, purpose="checkpoint-cleanup"):
            result.cleaned_temp_files = cleanup_stale_temp_files(target.parent)
    except LockConflict:
        # Another writer is active.  Atomic replacements mean reads still see a
        # complete checkpoint; leave its temp files alone.
        pass
    previous_path = target.with_name(target.stem + ".prev" + target.suffix)
    payload: Optional[Dict[str, Any]] = None
    if target.exists():
        payload = _read_checkpoint_file(target)
        if payload is not None:
            result.recovered_from = "main"
    if payload is None and previous_path.exists():
        payload = _read_checkpoint_file(previous_path)
        if payload is not None:
            result.recovered_from = "backup"
            result.half_write_detected = True
            result.diagnostics.append(
                Diagnostic(
                    code=codes.E_HALF_WRITE,
                    message="primary checkpoint was missing/corrupt; recovered the previous complete checkpoint",
                    severity="warning",
                    source_file=str(previous_path),
                )
            )
    if payload is None:
        if target.exists() or previous_path.exists():
            result.reasons.append("checkpoint_corrupt")
            result.diagnostics.append(
                Diagnostic(
                    code=codes.E_CHECKPOINT_CORRUPT,
                    message="no complete checkpoint could be recovered; rebuilding from inputs",
                    severity="warning",
                    source_file=str(target),
                )
            )
        return result

    result.previous_trust_summary = list(payload.get("trust_summary") or [])
    result.previous_trust_version = payload.get("trust_view_version")
    result.sequence = int(payload.get("sequence", 0) or 0)
    result.aggregate_digest = payload.get("aggregate_digest")
    ordered_ids = payload.get("ordered_connection_ids") or []
    if sorted(ordered_ids) != sorted((payload.get("completed") or {}).keys()):
        result.reasons.append("checkpoint_completed_index_mismatch")
        result.diagnostics.append(
            Diagnostic(
                code=codes.E_CHECKPOINT_CORRUPT,
                message="checkpoint completed index does not match its entries; rebuilding",
                source_file=str(target),
            )
        )
        return result

    hard_rebuild = False
    try:
        checkpoint_structure_version = int(payload.get("structure_version", -1))
    except (TypeError, ValueError):
        checkpoint_structure_version = -1
    if checkpoint_structure_version != codes.CHECKPOINT_STRUCTURE_VERSION:
        hard_rebuild = True
        result.reasons.append("checkpoint_structure_version_changed")
        result.diagnostics.append(
            Diagnostic(
                code=codes.E_CHECKPOINT_VERSION,
                message="checkpoint structure version changed; rebuilding from inputs",
                source_file=str(target),
                detail={
                    "checkpoint": payload.get("structure_version"),
                    "engine": codes.CHECKPOINT_STRUCTURE_VERSION,
                },
            )
        )
    if payload.get("merge_policy_version") != merge_policy_version:
        hard_rebuild = True
        result.reasons.append("merge_policy_version_changed")
        result.diagnostics.append(
            Diagnostic(
                code=codes.E_CHECKPOINT_VERSION,
                message="trust merge policy version changed; rebuilding from inputs",
                source_file=str(target),
                detail={
                    "checkpoint": payload.get("merge_policy_version"),
                    "engine": merge_policy_version,
                },
            )
        )
    if payload.get("sequence_policy_version") != codes.SEQUENCE_POLICY_VERSION:
        hard_rebuild = True
        result.reasons.append("sequence_policy_version_changed")
    if payload.get("input_fingerprint") != expected_input_fingerprint:
        hard_rebuild = True
        result.reasons.append("input_fingerprint_changed")
        result.diagnostics.append(
            Diagnostic(
                code=codes.E_CHECKPOINT_INPUT_CHANGED,
                message="input content changed since the checkpoint; rebuilding from inputs",
                severity="warning",
                source_file=str(target),
            )
        )
    if payload.get("config_fingerprint") != expected_config_fingerprint:
        hard_rebuild = True
        result.reasons.append("config_fingerprint_changed")
    if payload.get("trust_view_version") != expected_trust_version:
        hard_rebuild = True
        result.reasons.append("trust_view_version_changed")

    if hard_rebuild:
        result.usable = False
        result.rebuild = True
        return result

    completed_raw = payload.get("completed") or {}
    completed: Dict[str, ConnectionResult] = {}
    for conn_id in sorted(completed_raw):
        entry = completed_raw[conn_id]
        if not isinstance(entry, dict) or not isinstance(entry.get("result"), dict):
            result.reasons.append("checkpoint_completed_entry_invalid")
            result.diagnostics.append(
                Diagnostic(
                    code=codes.E_CHECKPOINT_CORRUPT,
                    message="checkpoint completed entry is malformed; rebuilding",
                    source_file=str(target),
                    connection_id=conn_id,
                )
            )
            return result
        restored = ConnectionResult.from_dict(entry["result"])
        calculated = restored.compute_digest()
        if calculated != entry.get("digest"):
            result.reasons.append("checkpoint_result_digest_mismatch")
            result.diagnostics.append(
                Diagnostic(
                    code=codes.E_CHECKPOINT_CORRUPT,
                    message="stored connection result digest does not match its payload; rebuilding",
                    source_file=str(target),
                    connection_id=conn_id,
                )
            )
            return result
        completed[conn_id] = restored
    calculated_aggregate = digest_value(
        [(conn_id, completed[conn_id].conclusion_digest) for conn_id in sorted(completed)]
    )
    if payload.get("aggregate_digest") != calculated_aggregate:
        result.reasons.append("checkpoint_aggregate_digest_mismatch")
        result.diagnostics.append(
            Diagnostic(
                code=codes.E_CHECKPOINT_CORRUPT,
                message="checkpoint aggregate digest does not match completed entries; rebuilding",
                source_file=str(target),
            )
        )
        return result
    result.completed = completed
    result.usable = True
    result.rebuild = False
    return result
