from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable

from .errors import AppError, ErrorCode
from .models import WalRow
from .util import canonical_json, sha256_bytes, sha256_obj

_SEGMENT_RE = re.compile(r"^wal-(\d+)\.jsonl$")
_TABLE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,127}$")
_OPS = {"insert", "update", "delete"}


def compute_content_hash(record: dict[str, Any]) -> str:
    payload = {
        "txn_id": record["txn_id"],
        "table": record["table"],
        "key": record["key"],
        "op": record["op"],
        "before": record.get("before"),
        "after": record.get("after"),
        "checkpoint_gen": record["checkpoint_gen"],
        "commit_ms": record["commit_ms"],
    }
    return sha256_obj(payload)


def _invalid(path: Path, line_no: int, message: str, **details: Any) -> AppError:
    return AppError(
        ErrorCode.WAL_INVALID_ROW,
        message,
        {"segment": path.name, "line_no": line_no, **details},
    )


def _pick(raw: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in raw:
            return raw[name]
    return None


def _normalise_record(raw: dict[str, Any], path: Path, line_no: int) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise _invalid(path, line_no, "WAL line must be a JSON object")
    record = {
        "txn_id": _pick(raw, "txn_id", "tx_id", "transaction_id", "txid"),
        "table": _pick(raw, "table", "table_name"),
        "key": _pick(raw, "key", "row_key"),
        "op": _pick(raw, "op", "operation"),
        "before": _pick(raw, "before", "before_image"),
        "after": _pick(raw, "after", "after_image"),
        "checkpoint_gen": _pick(
            raw, "checkpoint_gen", "checkpoint_generation", "checkpoint"
        ),
        "content_hash": _pick(raw, "content_hash", "hash", "digest"),
        "commit_ms": _pick(raw, "commit_ms", "committed_at_ms", "commit_time_ms"),
    }
    return record


def _validate_record_values(record: dict[str, Any], path: Path, line_no: int) -> None:
    txn_id = record["txn_id"]
    if not isinstance(txn_id, int) or isinstance(txn_id, bool) or txn_id <= 0:
        raise _invalid(path, line_no, "txn_id must be a positive integer")
    table = record["table"]
    if not isinstance(table, str) or not _TABLE_RE.fullmatch(table):
        raise _invalid(
            path,
            line_no,
            "table must be a safe logical table name",
            table=table,
        )
    key = record["key"]
    if not isinstance(key, str) or not key:
        raise _invalid(path, line_no, "key must be a non-empty string")
    op = record["op"]
    if op not in _OPS:
        raise _invalid(
            path,
            line_no,
            "op is outside the closed enum {insert, update, delete}",
            op=op,
        )
    before = record["before"]
    after = record["after"]
    if before is not None and not isinstance(before, dict):
        raise _invalid(path, line_no, "before image must be an object or null")
    if after is not None and not isinstance(after, dict):
        raise _invalid(path, line_no, "after image must be an object or null")
    if op == "insert" and (before is not None or after is None):
        raise _invalid(
            path,
            line_no,
            "insert requires before=null and a non-null after image",
        )
    if op == "update" and (before is None or after is None):
        raise _invalid(
            path,
            line_no,
            "update requires non-null before and after images",
        )
    if op == "delete" and (before is None or after is not None):
        raise _invalid(
            path,
            line_no,
            "delete requires a non-null before image and after=null",
        )
    checkpoint_gen = record["checkpoint_gen"]
    if (
        not isinstance(checkpoint_gen, int)
        or isinstance(checkpoint_gen, bool)
        or checkpoint_gen <= 0
    ):
        raise _invalid(path, line_no, "checkpoint_gen must be a positive integer")
    commit_ms = record["commit_ms"]
    if (
        not isinstance(commit_ms, int)
        or isinstance(commit_ms, bool)
        or commit_ms < 0
    ):
        raise _invalid(path, line_no, "commit_ms must be a non-negative integer")


def parse_wal_file(path: Path) -> list[WalRow]:
    match = _SEGMENT_RE.fullmatch(path.name)
    if match is None:
        raise AppError(
            ErrorCode.WAL_CHAIN_BROKEN,
            "WAL segment filename must be wal-NNNNNN.jsonl",
            {"segment": path.name},
        )
    segment_no = int(match.group(1))
    rows: list[WalRow] = []
    previous_txn: int | None = None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise AppError(
            ErrorCode.IO_ERROR,
            "could not read WAL segment",
            {"segment": str(path), "reason": str(exc)},
        ) from exc
    for line_no, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise _invalid(path, line_no, "WAL line is not valid JSON", reason=str(exc))
        record = _normalise_record(raw, path, line_no)
        _validate_record_values(record, path, line_no)
        expected_hash = compute_content_hash(record)
        if record["content_hash"] != expected_hash:
            raise AppError(
                ErrorCode.WAL_HASH_MISMATCH,
                "WAL content hash does not match the row content",
                {
                    "segment": path.name,
                    "line_no": line_no,
                    "txn_id": record["txn_id"],
                    "expected": expected_hash,
                    "actual": record["content_hash"],
                },
            )
        if previous_txn is not None and record["txn_id"] <= previous_txn:
            raise AppError(
                ErrorCode.WAL_CHAIN_BROKEN,
                "transaction ids must be strictly increasing within a segment",
                {
                    "segment": path.name,
                    "line_no": line_no,
                    "previous_txn_id": previous_txn,
                    "txn_id": record["txn_id"],
                },
            )
        previous_txn = record["txn_id"]
        rows.append(
            WalRow(
                segment=path.name,
                segment_no=segment_no,
                line_no=line_no,
                txn_id=record["txn_id"],
                table=record["table"],
                key=record["key"],
                op=record["op"],
                before=record["before"],
                after=record["after"],
                checkpoint_gen=record["checkpoint_gen"],
                content_hash=record["content_hash"],
                commit_ms=record["commit_ms"],
            )
        )
    if not rows:
        raise AppError(
            ErrorCode.WAL_CHAIN_BROKEN,
            "WAL segment contains no rows",
            {"segment": path.name},
        )
    return rows


def discover_wal_files(wal_dir: Path) -> list[Path]:
    if not wal_dir.is_dir():
        raise AppError(
            ErrorCode.WAL_CHAIN_BROKEN,
            "WAL directory does not exist",
            {"wal_dir": str(wal_dir)},
        )
    files = list(wal_dir.glob("wal-*.jsonl"))
    if not files:
        raise AppError(
            ErrorCode.WAL_CHAIN_BROKEN,
            "no WAL segments found",
            {"wal_dir": str(wal_dir)},
        )
    parsed: list[tuple[int, Path]] = []
    for path in files:
        match = _SEGMENT_RE.fullmatch(path.name)
        if match is None:
            raise AppError(
                ErrorCode.WAL_CHAIN_BROKEN,
                "WAL segment filename must be wal-NNNNNN.jsonl",
                {"segment": path.name},
            )
        parsed.append((int(match.group(1)), path))
    parsed.sort()
    numbers = [number for number, _ in parsed]
    if len(set(numbers)) != len(numbers):
        raise AppError(
            ErrorCode.WAL_CHAIN_BROKEN,
            "duplicate WAL segment sequence number",
            {"segment_numbers": numbers},
        )
    if numbers != list(range(numbers[0], numbers[0] + len(numbers))):
        raise AppError(
            ErrorCode.WAL_CHAIN_BROKEN,
            "WAL segment numbers must be contiguous",
            {"segment_numbers": numbers},
        )
    return [path for _, path in parsed]


def load_wal(wal_dir: Path) -> list[WalRow]:
    files = discover_wal_files(wal_dir)
    rows: list[WalRow] = []
    previous_txn: int | None = None
    previous_segment_no: int | None = None
    for path in files:
        segment_rows = parse_wal_file(path)
        first_txn = segment_rows[0].txn_id
        if previous_segment_no is not None and segment_rows[0].segment_no <= previous_segment_no:
            raise AppError(
                ErrorCode.WAL_CHAIN_BROKEN,
                "WAL segment ordering is not strictly increasing",
                {"segment": path.name},
            )
        if previous_txn is not None and first_txn <= previous_txn:
            raise AppError(
                ErrorCode.WAL_CHAIN_BROKEN,
                "transaction ids must continue across WAL segments without overlap",
                {
                    "segment": path.name,
                    "previous_txn_id": previous_txn,
                    "txn_id": first_txn,
                },
            )
        rows.extend(segment_rows)
        previous_txn = segment_rows[-1].txn_id
        previous_segment_no = segment_rows[0].segment_no
    validate_wal_ordering(rows)
    return rows


def validate_wal_ordering(rows: Iterable[WalRow]) -> None:
    previous: WalRow | None = None
    for row in rows:
        if previous is not None:
            if row.segment_no < previous.segment_no:
                raise AppError(
                    ErrorCode.WAL_CHAIN_BROKEN,
                    "WAL rows are not in segment order",
                    {"txn_id": row.txn_id},
                )
            if row.segment_no == previous.segment_no and row.line_no <= previous.line_no:
                raise AppError(
                    ErrorCode.WAL_CHAIN_BROKEN,
                    "WAL rows are not in line order",
                    {"txn_id": row.txn_id},
                )
            if row.txn_id <= previous.txn_id:
                raise AppError(
                    ErrorCode.WAL_CHAIN_BROKEN,
                    "transaction ids must be strictly increasing across the WAL chain",
                    {
                        "previous_txn_id": previous.txn_id,
                        "txn_id": row.txn_id,
                    },
                )
        previous = row


def validate_wal_dependencies(
    rows: Iterable[WalRow], checkpoint_generations: set[int]
) -> None:
    for row in rows:
        if row.checkpoint_gen not in checkpoint_generations:
            raise AppError(
                ErrorCode.WAL_CHECKPOINT_DEPENDENCY,
                "WAL row depends on a checkpoint generation absent from the chain",
                {
                    "txn_id": row.txn_id,
                    "checkpoint_gen": row.checkpoint_gen,
                    "known_generations": sorted(checkpoint_generations),
                    "segment": row.segment,
                    "line_no": row.line_no,
                },
            )


def row_from_db(values: dict[str, Any]) -> WalRow:
    return WalRow(
        segment=values["segment"],
        segment_no=int(values["segment_no"]),
        line_no=int(values["line_no"]),
        txn_id=int(values["txn_id"]),
        table=values["table_name"],
        key=values["row_key"],
        op=values["op"],
        before=json.loads(values["before_json"]) if values["before_json"] else None,
        after=json.loads(values["after_json"]) if values["after_json"] else None,
        checkpoint_gen=int(values["checkpoint_gen"]),
        content_hash=values["content_hash"],
        commit_ms=int(values["commit_ms"]),
    )


def validate_rows_against_hashes(rows: Iterable[WalRow]) -> None:
    for row in rows:
        expected = compute_content_hash(row.as_record())
        if expected != row.content_hash:
            raise AppError(
                ErrorCode.WAL_HASH_MISMATCH,
                "WAL content hash does not match the row content",
                {
                    "segment": row.segment,
                    "line_no": row.line_no,
                    "txn_id": row.txn_id,
                    "expected": expected,
                    "actual": row.content_hash,
                },
            )


def validate_wal_rows(rows: Iterable[WalRow]) -> None:
    for row in rows:
        record = row.as_record()
        _validate_record_values(record, Path(row.segment), row.line_no)
        expected = compute_content_hash(record)
        if expected != row.content_hash:
            raise AppError(
                ErrorCode.WAL_HASH_MISMATCH,
                "WAL content hash does not match the row content",
                {
                    "segment": row.segment,
                    "line_no": row.line_no,
                    "txn_id": row.txn_id,
                    "expected": expected,
                    "actual": row.content_hash,
                },
            )


def wal_payload_bytes(rows: Iterable[WalRow]) -> bytes:
    return canonical_json([row.as_record() for row in rows]).encode("utf-8")


def wal_payload_hash(rows: Iterable[WalRow]) -> str:
    return sha256_bytes(wal_payload_bytes(rows))
