from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .config import canonical_json, stable_hash
from .errors import fail
from .locks import FileLock


class DataFiles:
    """Append-only partition files with duplicate checks and partial-tail repair."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, partition: int) -> Path:
        return self.root / f"partition-{partition:04d}.jsonl"

    def relative_path(self, partition: int) -> str:
        return str(Path("data") / f"partition-{partition:04d}.jsonl")

    def lock_path(self, partition: int) -> Path:
        return self.root / f"partition-{partition:04d}.lock"

    @staticmethod
    def record_hash(record: dict[str, Any]) -> str:
        body = {key: value for key, value in record.items() if key != "record_hash"}
        return stable_hash(body)

    def _read_unlocked(
        self, handle: Any, *, repair: bool
    ) -> tuple[list[dict[str, Any]], int, int]:
        handle.seek(0)
        data = handle.read()
        records: list[dict[str, Any]] = []
        offset = 0
        complete_end = 0
        partial_bytes = 0
        while True:
            newline = data.find(b"\n", offset)
            if newline < 0:
                partial_bytes = len(data) - complete_end
                break
            raw = data[offset:newline]
            line_start = offset
            offset = newline + 1
            complete_end = offset
            if not raw.strip():
                continue
            try:
                record = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                fail(
                    "E_DATA_CORRUPT",
                    "partition data file contains an invalid complete record",
                    error=str(exc),
                    byte_offset=line_start,
                )
            if not isinstance(record, dict) or "sample_id" not in record:
                fail(
                    "E_DATA_CORRUPT",
                    "partition data record is missing sample_id",
                    byte_offset=line_start,
                )
            expected = record.get("record_hash")
            actual = self.record_hash(record)
            if expected != actual:
                fail(
                    "E_DATA_CORRUPT",
                    "partition data record hash mismatch",
                    sample_id=record.get("sample_id"),
                    byte_offset=line_start,
                )
            record["_offset"] = line_start
            records.append(record)
        if repair and partial_bytes:
            handle.seek(complete_end)
            handle.truncate()
            handle.flush()
            os.fsync(handle.fileno())
        return records, complete_end, partial_bytes

    def scan(self, partition: int, *, repair: bool = False) -> dict[str, Any]:
        path = self.path(partition)
        if not path.exists():
            return {
                "partition": partition,
                "records": [],
                "valid_bytes": 0,
                "partial_bytes": 0,
                "path": str(path),
            }
        with FileLock(self.lock_path(partition)):
            with path.open("r+b") as handle:
                records, valid_bytes, partial_bytes = self._read_unlocked(
                    handle, repair=repair
                )
        return {
            "partition": partition,
            "records": records,
            "valid_bytes": valid_bytes,
            "partial_bytes": partial_bytes,
            "path": str(path),
        }

    def append(self, partition: int, record: dict[str, Any]) -> dict[str, Any]:
        path = self.path(partition)
        with FileLock(self.lock_path(partition)):
            path.touch(exist_ok=True)
            with path.open("r+b") as handle:
                records, valid_bytes, partial_bytes = self._read_unlocked(
                    handle, repair=True
                )
                for existing in records:
                    if existing["sample_id"] == record["sample_id"]:
                        return {
                            "appended": False,
                            "offset": int(existing["_offset"]),
                            "record_hash": existing["record_hash"],
                            "partial_bytes_repaired": partial_bytes,
                        }
                envelope = {
                    key: value for key, value in record.items() if key != "record_hash"
                }
                envelope["record_hash"] = self.record_hash(envelope)
                line = (canonical_json(envelope) + "\n").encode("utf-8")
                handle.seek(valid_bytes)
                offset = handle.tell()
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
                return {
                    "appended": True,
                    "offset": offset,
                    "record_hash": envelope["record_hash"],
                    "partial_bytes_repaired": partial_bytes,
                }

