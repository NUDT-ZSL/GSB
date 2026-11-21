from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable, Mapping, Sequence

from .errors import bad_wal

ZERO_HASH = "0" * 64


def canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise bad_wal("value is not canonical JSON", reason=str(exc)) from exc


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_text(canonical_json(value))


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def operation_content(
    table: str,
    row_key: str,
    operation: str,
    before: Any,
    after: Any,
) -> dict[str, Any]:
    return {
        "after": after,
        "before": before,
        "operation": operation,
        "row_key": row_key,
        "table": table,
    }


def operation_digest(
    table: str,
    row_key: str,
    operation: str,
    before: Any,
    after: Any,
) -> str:
    return sha256_json(operation_content(table, row_key, operation, before, after))


def tx_digest(record: Mapping[str, Any]) -> str:
    return sha256_json({key: value for key, value in record.items() if key != "tx_hash"})


def row_digest(
    table: str,
    row_key: str,
    payload: Any,
    version: int,
    source_tx: int,
    tombstone: bool | int,
) -> str:
    return sha256_json(
        {
            "key": row_key,
            "payload": payload,
            "source_tx": source_tx,
            "table": table,
            "tombstone": bool(tombstone),
            "version": version,
        }
    )


def table_summary(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    ordered = sorted(rows, key=lambda row: str(row["key"]))
    digest_rows = [
        {
            "key": row["key"],
            "row_hash": row["row_hash"],
        }
        for row in ordered
    ]
    live_rows = [row for row in ordered if not bool(row["tombstone"])]
    versions = [int(row["version"]) for row in ordered]
    return {
        "live_row_count": len(live_rows),
        "max_version": max(versions, default=0),
        "row_count": len(ordered),
        "version_digest": sha256_json(digest_rows),
    }


def summarize_tables(
    rows_by_table: Mapping[str, Sequence[Mapping[str, Any]]],
) -> dict[str, dict[str, Any]]:
    return {
        table: table_summary(rows)
        for table, rows in sorted(rows_by_table.items())
    }


def state_digest(
    rows_by_table: Mapping[str, Sequence[Mapping[str, Any]]],
) -> str:
    return sha256_json(summarize_tables(rows_by_table))


def page_for_key(table: str, key: str) -> str:
    digest = sha256_text(f"{table}\x00{key}")
    return f"p{int(digest[:8], 16) % 128:04d}"


def dirty_pages(
    rows_by_table: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    changed_after_tx: int | None = None,
) -> dict[str, dict[str, list[str]]]:
    result: dict[str, dict[str, list[str]]] = {}
    for table, rows in sorted(rows_by_table.items()):
        pages: dict[str, list[str]] = {}
        for row in sorted(rows, key=lambda item: str(item["key"])):
            if changed_after_tx is not None and int(row["source_tx"]) <= changed_after_tx:
                continue
            pages.setdefault(page_for_key(table, str(row["key"])), []).append(str(row["key"]))
        result[table] = {page: sorted(keys) for page, keys in sorted(pages.items())}
    return result


def stable_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        indent=2,
        sort_keys=True,
    )
