"""Ledger/result snapshot encoding and checkpoint bundle validation."""

from __future__ import annotations

from typing import Any

from .canonical import decode_array_f64, encode_array_f64, hash_array_f64, sha256_json
from .codes import E_SNAPSHOT_CORRUPT
from .errors import raise_business
from .models import Ledger, RowBlock
from .snapshots import (
    LEDGER_CATEGORY,
    RESULT_CATEGORY,
    VerifiedSnapshot,
    make_snapshot,
    verify_snapshot,
)


CHECKPOINT_SCHEMA = "sparse-inc-checkpoint/v1"


def encode_ledger_snapshot(ledger: Ledger) -> dict[str, Any]:
    return make_snapshot(LEDGER_CATEGORY, ledger.version, ledger.payload())


def decode_ledger_snapshot(raw: Any, *, case_id: str) -> tuple[Ledger, VerifiedSnapshot]:
    verified = verify_snapshot(raw, expected_category=LEDGER_CATEGORY, case_id=case_id)
    payload = verified.payload
    if not isinstance(payload, dict):
        _corrupt(case_id, LEDGER_CATEGORY, verified.version, "ledger payload must be an object")
    accepted = payload.get("accepted")
    row_owners = payload.get("row_owners")
    if not isinstance(accepted, list) or not isinstance(row_owners, list):
        _corrupt(case_id, LEDGER_CATEGORY, verified.version, "ledger payload lists are invalid")
    ledger = Ledger(
        version=verified.version,
        accepted=tuple(dict(item) for item in accepted),
        row_owners=tuple(dict(item) for item in row_owners),
    )
    return ledger, verified


def encode_result_snapshot(version: int, blocks: tuple[RowBlock, ...]) -> dict[str, Any]:
    payload = {
        "blocks": [
            {
                "block_id": block.block_id,
                "row_start": block.row_start,
                "row_end": block.row_end,
                "last_write_version": block.last_write_version,
                "input_digest": block.input_digest,
                "result_digest": block.digest,
                "result": encode_array_f64(block.result),
            }
            for block in blocks
        ]
    }
    return make_snapshot(RESULT_CATEGORY, version, payload)


def decode_result_snapshot(
    raw: Any,
    *,
    case_id: str,
) -> tuple[tuple[RowBlock, ...], VerifiedSnapshot]:
    verified = verify_snapshot(raw, expected_category=RESULT_CATEGORY, case_id=case_id)
    payload = verified.payload
    if not isinstance(payload, dict) or not isinstance(payload.get("blocks"), list):
        _corrupt(case_id, RESULT_CATEGORY, verified.version, "result payload blocks are invalid")
    blocks: list[RowBlock] = []
    for index, item in enumerate(payload["blocks"]):
        if not isinstance(item, dict):
            _corrupt(case_id, RESULT_CATEGORY, verified.version, f"block {index} is not an object")
        try:
            array = decode_array_f64(item["result"])
        except (KeyError, TypeError, ValueError) as exc:
            _corrupt(
                case_id,
                RESULT_CATEGORY,
                verified.version,
                f"block {index} array decode failed: {exc}",
            )
        digest = hash_array_f64(array)
        if digest != item.get("result_digest"):
            _corrupt(
                case_id,
                RESULT_CATEGORY,
                verified.version,
                f"block {index} result digest mismatch",
                block_id=item.get("block_id"),
            )
        try:
            block = RowBlock(
                block_id=str(item["block_id"]),
                row_start=int(item["row_start"]),
                row_end=int(item["row_end"]),
                result=array,
                digest=digest,
                input_digest=str(item["input_digest"]),
                last_write_version=int(item["last_write_version"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            _corrupt(
                case_id,
                RESULT_CATEGORY,
                verified.version,
                f"block {index} metadata decode failed: {exc}",
            )
        blocks.append(block)
    return tuple(blocks), verified


def make_checkpoint_bundle(
    *,
    case_id: str,
    operation: str,
    base_checksum: str,
    config_checksum: str,
    next_write_index: int,
    ledger_snapshot: dict[str, Any],
    result_snapshot: dict[str, Any],
    incremental_events: list[dict[str, Any]],
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "schema_version": CHECKPOINT_SCHEMA,
        "case_id": case_id,
        "operation": operation,
        "base_checksum": base_checksum,
        "config_checksum": config_checksum,
        "next_write_index": int(next_write_index),
        "ledger": ledger_snapshot,
        "results": result_snapshot,
        "incremental_events": incremental_events,
    }
    body["bundle_checksum"] = sha256_json(body)
    return body


def verify_checkpoint_bundle(
    raw: Any,
    *,
    case_id: str,
    operation: str,
    base_checksum: str,
    config_checksum: str,
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        _corrupt(case_id, "checkpoint", None, "checkpoint bundle must be an object")
    verify_snapshot(raw.get("ledger"), expected_category=LEDGER_CATEGORY, case_id=case_id)
    verify_snapshot(raw.get("results"), expected_category=RESULT_CATEGORY, case_id=case_id)
    provided = raw.get("bundle_checksum")
    body = dict(raw)
    body.pop("bundle_checksum", None)
    expected = sha256_json(body)
    if provided != expected:
        _corrupt(
            case_id,
            "checkpoint",
            _version_hint(raw),
            "checkpoint bundle checksum mismatch",
            expected_checksum=expected,
            provided_checksum=provided,
        )
    if raw.get("schema_version") != CHECKPOINT_SCHEMA:
        _corrupt(case_id, "checkpoint", _version_hint(raw), "unsupported checkpoint schema")
    if raw.get("case_id") != case_id:
        _corrupt(case_id, "checkpoint", _version_hint(raw), "checkpoint case_id mismatch")
    if raw.get("operation") != operation:
        _corrupt(case_id, "checkpoint", _version_hint(raw), "checkpoint operation mismatch")
    if raw.get("base_checksum") != base_checksum:
        _corrupt(case_id, "checkpoint", _version_hint(raw), "checkpoint base matrix checksum mismatch")
    if raw.get("config_checksum") != config_checksum:
        _corrupt(case_id, "checkpoint", _version_hint(raw), "checkpoint config checksum mismatch")
    next_index = raw.get("next_write_index")
    if isinstance(next_index, bool) or not isinstance(next_index, int) or next_index < 0:
        _corrupt(case_id, "checkpoint", _version_hint(raw), "checkpoint next_write_index is invalid")
    if not isinstance(raw.get("incremental_events"), list):
        _corrupt(case_id, "checkpoint", _version_hint(raw), "checkpoint incremental_events is invalid")
    return raw


def _version_hint(raw: Any) -> int | None:
    if isinstance(raw, dict):
        ledger = raw.get("ledger")
        if isinstance(ledger, dict) and isinstance(ledger.get("version"), int):
            return int(ledger["version"])
    return None


def _corrupt(
    case_id: str | None,
    category: str,
    version: int | None,
    message: str,
    **details: Any,
) -> None:
    raise_business(
        E_SNAPSHOT_CORRUPT,
        case_id=case_id,
        segment=f"snapshot.{category}",
        message=message,
        snapshot_category=category,
        snapshot_version=version,
        **details,
    )
