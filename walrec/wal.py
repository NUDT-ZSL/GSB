from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .canonical import ZERO_HASH, operation_digest, tx_digest
from .errors import bad_wal, chain_break, summary_mismatch
from .models import WalRecord

WAL_FILE = re.compile(r"^wal-(\d+)\.jsonl$")
OPERATIONS = {"INSERT", "UPDATE", "DELETE"}
HEX64 = re.compile(r"^[0-9a-f]{64}$")


def discover_wal_files(sample_root: Path) -> list[Path]:
    wal_dir = sample_root / "wal"
    files: list[Path] = []
    if wal_dir.is_dir():
        files.extend(wal_dir.glob("wal-*.jsonl"))
    files.extend(sample_root.glob("wal-*.jsonl"))
    if not files:
        raise bad_wal("no WAL segment files were found", sample=str(sample_root.name))
    numbered: list[tuple[int, Path]] = []
    seen: set[int] = set()
    for path in files:
        match = WAL_FILE.fullmatch(path.name)
        if not match:
            raise bad_wal("WAL filename is invalid", path=path.name)
        number = int(match.group(1))
        if number in seen:
            raise bad_wal("duplicate WAL segment number", segment_no=number)
        seen.add(number)
        numbered.append((number, path))
    numbered.sort()
    expected = list(range(1, len(numbered) + 1))
    actual = [number for number, _ in numbered]
    if actual != expected:
        raise chain_break(
            "WAL segment sequence is not contiguous from 1",
            expected=expected,
            actual=actual,
        )
    return [path for _, path in numbered]


def _require_int(value: Any, field: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise bad_wal(f"{field} must be an integer >= {minimum}", field=field)
    return value


def _require_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise bad_wal(f"{field} must be a non-empty string", field=field)
    return value


def _require_hash(value: Any, field: str) -> str:
    if not isinstance(value, str) or not HEX64.fullmatch(value):
        raise bad_wal(f"{field} must be a lowercase SHA-256 digest", field=field)
    return value


def _validate_images(operation: str, before: Any, after: Any) -> None:
    if operation == "INSERT":
        if before is not None or not isinstance(after, dict):
            raise bad_wal("INSERT requires before=null and an object after image")
    elif operation == "UPDATE":
        if not isinstance(before, dict) or not isinstance(after, dict):
            raise bad_wal("UPDATE requires object before and after images")
    elif operation == "DELETE":
        if not isinstance(before, dict) or after is not None:
            raise bad_wal("DELETE requires an object before image and after=null")


def _parse_record(raw: Any, *, segment_no: int, line_no: int, path: Path) -> WalRecord:
    if not isinstance(raw, dict):
        raise bad_wal(
            "WAL line must be a JSON object",
            segment_no=segment_no,
            line_no=line_no,
        )
    try:
        record_segment = _require_int(raw.get("segment_no"), "segment_no", minimum=1)
        tx_id = _require_int(raw.get("logical_tx_id"), "logical_tx_id", minimum=1)
        prev_tx_id = _require_int(raw.get("prev_tx_id"), "prev_tx_id")
        prev_tx_hash = _require_hash(raw.get("prev_tx_hash"), "prev_tx_hash")
        checkpoint_gen = _require_int(raw.get("checkpoint_gen"), "checkpoint_gen", minimum=1)
        table = _require_string(raw.get("table"), "table")
        row_key = _require_string(raw.get("row_key"), "row_key")
        operation = _require_string(raw.get("operation"), "operation")
        duration_ms = _require_int(raw.get("duration_ms"), "duration_ms", minimum=1)
        content_hash = _require_hash(raw.get("content_hash"), "content_hash")
        tx_hash = _require_hash(raw.get("tx_hash"), "tx_hash")
        before = raw.get("before")
        after = raw.get("after")
    except bad_wal as exc:
        exc.details.update({"segment_no": segment_no, "line_no": line_no})
        raise

    if record_segment != segment_no:
        raise bad_wal(
            "record segment_no differs from its file",
            segment_no=segment_no,
            line_no=line_no,
            record_segment_no=record_segment,
        )
    if operation not in OPERATIONS:
        raise bad_wal(
            "operation is outside the closed enum",
            segment_no=segment_no,
            line_no=line_no,
            operation=operation,
        )
    _validate_images(operation, before, after)
    expected_content = operation_digest(table, row_key, operation, before, after)
    if expected_content != content_hash:
        raise summary_mismatch(
            "WAL operation content hash mismatch",
            segment_no=segment_no,
            line_no=line_no,
            txid=tx_id,
            expected=expected_content,
            actual=content_hash,
        )
    expected_tx = tx_digest(raw)
    if expected_tx != tx_hash:
        raise summary_mismatch(
            "WAL transaction hash mismatch",
            segment_no=segment_no,
            line_no=line_no,
            txid=tx_id,
            expected=expected_tx,
            actual=tx_hash,
        )
    return WalRecord(
        segment_no=segment_no,
        logical_tx_id=tx_id,
        prev_tx_id=prev_tx_id,
        prev_tx_hash=prev_tx_hash,
        checkpoint_gen=checkpoint_gen,
        table=table,
        row_key=row_key,
        operation=operation,
        before=before,
        after=after,
        duration_ms=duration_ms,
        content_hash=content_hash,
        tx_hash=tx_hash,
        raw=dict(raw),
    )


def parse_wal(sample_root: Path) -> list[WalRecord]:
    records: list[WalRecord] = []
    for path in discover_wal_files(sample_root):
        match = WAL_FILE.fullmatch(path.name)
        assert match is not None
        segment_no = int(match.group(1))
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line_no, line in enumerate(handle, start=1):
                    if not line.strip():
                        raise bad_wal(
                            "blank WAL lines are not permitted",
                            segment_no=segment_no,
                            line_no=line_no,
                        )
                    try:
                        raw = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise bad_wal(
                            "malformed WAL JSON line",
                            segment_no=segment_no,
                            line_no=line_no,
                            reason=str(exc),
                        ) from exc
                    records.append(
                        _parse_record(
                            raw,
                            segment_no=segment_no,
                            line_no=line_no,
                            path=path,
                        )
                    )
        except UnicodeDecodeError as exc:
            raise bad_wal(
                "WAL segment is not UTF-8",
                segment_no=segment_no,
                reason=str(exc),
            ) from exc

    if not records:
        raise bad_wal("WAL contains no transaction records")

    previous: WalRecord | None = None
    seen: set[int] = set()
    for record in records:
        if record.logical_tx_id in seen:
            raise chain_break("duplicate logical transaction id", txid=record.logical_tx_id)
        seen.add(record.logical_tx_id)
        if previous is not None:
            if record.logical_tx_id <= previous.logical_tx_id:
                raise chain_break(
                    "logical transaction ids must strictly increase",
                    previous_txid=previous.logical_tx_id,
                    txid=record.logical_tx_id,
                )
            if record.prev_tx_id != previous.logical_tx_id:
                raise chain_break(
                    "WAL prev_tx_id does not reference the preceding transaction",
                    previous_txid=previous.logical_tx_id,
                    prev_tx_id=record.prev_tx_id,
                    txid=record.logical_tx_id,
                )
            if record.prev_tx_hash != previous.tx_hash:
                raise chain_break(
                    "WAL prev_tx_hash does not match the preceding transaction hash",
                    previous_txid=previous.logical_tx_id,
                    txid=record.logical_tx_id,
                    expected=previous.tx_hash,
                    actual=record.prev_tx_hash,
                )
        elif record.prev_tx_id == 0 and record.prev_tx_hash != ZERO_HASH:
            raise chain_break(
                "first WAL transaction must chain from the zero hash",
                txid=record.logical_tx_id,
                expected=ZERO_HASH,
                actual=record.prev_tx_hash,
            )
        previous = record
    return records
