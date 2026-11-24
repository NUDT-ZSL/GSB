"""Checksummed, atomically replaced checkpoints and ledger recovery helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .atomic import atomic_write_json, read_json
from .codes import (
    E_BAD_SCHEMA,
    E_CHECKPOINT_INVALID,
    E_CHECKPOINT_MISMATCH,
    E_OK,
    SCHEMA_VERSION,
)
from .models import AuditError, canonical_hash

CHECKPOINT_FORMAT = "firewall-audit-checkpoint"
_STATE_TABLES = {
    "meta": dict,
    "snapshot": list,
    "cases": list,
    "case_results": list,
    "adjudication_hits": list,
    "pending_cases": list,
}


def state_fingerprint(state: dict[str, Any]) -> str:
    return canonical_hash(state)


def make_checkpoint(state: dict[str, Any], *, seq: int) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "format": CHECKPOINT_FORMAT,
        "schema_version": SCHEMA_VERSION,
        "seq": seq,
        "state_fingerprint": state_fingerprint(state),
        "state": state,
    }
    payload["checksum"] = canonical_hash(payload)
    return payload


def write_checkpoint(
    path: str | Path,
    state: dict[str, Any],
    *,
    seq: int,
) -> dict[str, Any]:
    checkpoint = make_checkpoint(state, seq=seq)
    atomic_write_json(path, checkpoint)
    return checkpoint


def load_checkpoint(path: str | Path) -> dict[str, Any]:
    try:
        value = read_json(path)
    except (OSError, ValueError) as exc:
        raise AuditError(
            E_CHECKPOINT_INVALID,
            f"cannot read checkpoint: {exc}",
            source_category="checkpoint",
            detail={"path": str(path)},
        ) from exc
    if not isinstance(value, dict):
        raise AuditError(
            E_CHECKPOINT_INVALID,
            "checkpoint must be a JSON object",
            source_category="checkpoint",
        )
    if value.get("format") != CHECKPOINT_FORMAT:
        raise AuditError(
            E_CHECKPOINT_INVALID,
            "checkpoint format marker is invalid",
            source_category="checkpoint",
        )
    schema_version = value.get("schema_version", -1)
    if not isinstance(schema_version, int) or isinstance(
        schema_version, bool
    ) or int(schema_version) != SCHEMA_VERSION:
        raise AuditError(
            E_CHECKPOINT_INVALID,
            "checkpoint schema_version is unsupported",
            source_category="checkpoint",
        )
    claimed_checksum = value.get("checksum")
    body = {key: item for key, item in value.items() if key != "checksum"}
    if claimed_checksum != canonical_hash(body):
        raise AuditError(
            E_CHECKPOINT_INVALID,
            "checkpoint checksum does not match",
            source_category="checkpoint",
        )
    state = value.get("state")
    if not isinstance(state, dict):
        raise AuditError(
            E_CHECKPOINT_INVALID,
            "checkpoint state must be an object",
            source_category="checkpoint",
        )
    for table, expected_type in _STATE_TABLES.items():
        if table not in state or not isinstance(state[table], expected_type):
            raise AuditError(
                E_CHECKPOINT_INVALID,
                f"checkpoint state table {table!r} is missing or has a wrong type",
                source_category="checkpoint",
            )
    if value.get("state_fingerprint") != state_fingerprint(state):
        raise AuditError(
            E_CHECKPOINT_INVALID,
            "checkpoint state fingerprint does not match",
            source_category="checkpoint",
        )
    if (
        not isinstance(value.get("seq"), int)
        or isinstance(value.get("seq"), bool)
    ):
        raise AuditError(
            E_BAD_SCHEMA,
            "checkpoint seq must be an integer",
            source_category="checkpoint",
        )
    return value


def verify_against_state(
    checkpoint: dict[str, Any],
    ledger_state: dict[str, Any],
    ledger_seq: int,
) -> dict[str, Any]:
    checkpoint_fingerprint = checkpoint["state_fingerprint"]
    current_fingerprint = state_fingerprint(ledger_state)
    matches = (
        checkpoint_fingerprint == current_fingerprint
        and int(checkpoint["seq"]) == int(ledger_seq)
    )
    return {
        "valid": True,
        "matches_ledger": matches,
        "code": E_OK if matches else E_CHECKPOINT_MISMATCH,
        "checkpoint_seq": int(checkpoint["seq"]),
        "ledger_seq": int(ledger_seq),
        "checkpoint_state_fingerprint": checkpoint_fingerprint,
        "ledger_state_fingerprint": current_fingerprint,
    }
