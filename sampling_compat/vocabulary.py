"""Tokenizer vocabulary table loading and integrity validation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .errors import BusinessError, ErrorCode
from .models import VocabEntry, VocabTable
from .utils import checksum_obj


def load_vocab_table(path: Path) -> VocabTable:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BusinessError(
            ErrorCode.SNAPSHOT_CORRUPT,
            f"vocabulary table cannot be loaded: {exc}",
            entry="vocab_table",
            version="unknown",
        ) from exc

    snapshot_version = raw.get("snapshot_version") if isinstance(raw, dict) else None
    if not isinstance(snapshot_version, int) or snapshot_version < 1:
        raise BusinessError(
            ErrorCode.SNAPSHOT_CORRUPT,
            "vocabulary table has no valid snapshot version",
            entry="vocab_table",
            version=snapshot_version,
        )
    entries_raw = raw.get("entries")
    expected_checksum = raw.get("checksum")
    if not isinstance(entries_raw, dict) or not isinstance(expected_checksum, str):
        raise BusinessError(
            ErrorCode.SNAPSHOT_CORRUPT,
            "vocabulary table snapshot is missing entries or checksum",
            entry="vocab_table",
            version=snapshot_version,
        )
    actual_checksum = checksum_obj(entries_raw)
    if actual_checksum != expected_checksum:
        raise BusinessError(
            ErrorCode.SNAPSHOT_CORRUPT,
            "vocabulary table checksum does not match its content",
            entry="vocab_table",
            version=snapshot_version,
            details={"expected": expected_checksum, "actual": actual_checksum},
        )

    entries: dict[str, VocabEntry] = {}
    for version, entry_raw in sorted(entries_raw.items()):
        if not isinstance(version, str) or not version:
            raise BusinessError(
                ErrorCode.SNAPSHOT_CORRUPT,
                "vocabulary entry has an invalid version key",
                entry="vocab_table",
                version=snapshot_version,
            )
        if not isinstance(entry_raw, dict):
            raise BusinessError(
                ErrorCode.SNAPSHOT_CORRUPT,
                f"vocabulary entry '{version}' is not an object",
                entry="vocab_table",
                version=snapshot_version,
            )
        size = entry_raw.get("size")
        tokens = entry_raw.get("tokens")
        if (
            not isinstance(size, int)
            or size <= 0
            or not isinstance(tokens, list)
            or len(tokens) != size
            or not all(isinstance(token, str) for token in tokens)
        ):
            raise BusinessError(
                ErrorCode.SNAPSHOT_CORRUPT,
                f"vocabulary entry '{version}' has invalid size or token list",
                entry="vocab_table",
                version=snapshot_version,
            )
        entries[version] = VocabEntry(
            version=version,
            size=size,
            tokens=tuple(tokens),
        )
    if not entries:
        raise BusinessError(
            ErrorCode.SNAPSHOT_CORRUPT,
            "vocabulary table contains no versions",
            entry="vocab_table",
            version=snapshot_version,
        )
    return VocabTable(
        snapshot_version=snapshot_version,
        checksum=expected_checksum,
        entries=entries,
    )


def require_vocab_entry(
    table: VocabTable,
    version: str,
    *,
    request_id: str | None = None,
    field_name: str = "tokenizer_version",
) -> VocabEntry:
    entry = table.entries.get(version)
    if entry is None:
        raise BusinessError(
            ErrorCode.VOCAB_VERSION_MISMATCH,
            f"vocabulary version '{version}' is not present in the local table",
            request_id=request_id,
            field=field_name,
            original_value=version,
            details={"available_versions": sorted(table.entries)},
        )
    return entry


def vocab_table_snapshot_content(table: VocabTable) -> dict[str, Any]:
    return {
        "snapshot_version": table.snapshot_version,
        "checksum": table.checksum,
        "entries": {
            version: {
                "size": entry.size,
                "tokens": list(entry.tokens),
            }
            for version, entry in sorted(table.entries.items())
        },
    }
