"""Parser for the ordered local replay transaction stream."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .canonical import parse_tx_id, require_bool, require_int
from .errors import DiagnosticsError
from .models import LogRecord
from .util import read_jsonl, sha256_bytes, stable_json_bytes


def _segment_files(path: Path) -> list[Path]:
    if path.is_file():
        if path.suffix.lower() != ".jsonl":
            raise DiagnosticsError(
                "E_LOG_ORDER",
                message="replay log file must have .jsonl extension",
                location=str(path),
            )
        return [path]
    if not path.exists() or not path.is_dir():
        raise DiagnosticsError("E_IO", message="replay log directory is missing", location=str(path))
    files = sorted(
        (item for item in path.rglob("*.jsonl") if item.is_file()),
        key=lambda item: item.relative_to(path).as_posix(),
    )
    if not files:
        raise DiagnosticsError(
            "E_LOG_ORDER", message="replay log directory has no .jsonl segments", location=str(path)
        )
    return files


def parse_logs(path: Path) -> tuple[list[LogRecord], str]:
    records: list[LogRecord] = []
    previous_tx: int | None = None
    saw_unapplied = False
    digest_source: list[Any] = []
    sequence = 0

    for file_path in _segment_files(path):
        rows = read_jsonl(file_path)
        digest_source.append([file_path.name, len(rows)])
        for line_number, raw in rows:
            location = f"{file_path}:{line_number}"
            if not isinstance(raw, dict):
                raise DiagnosticsError(
                    "E_JSON", message="replay log line must be an object", location=location
                )
            sequence += 1
            tx = parse_tx_id(raw.get("tx_id"), location=f"{location}:tx_id")
            if previous_tx is not None:
                if tx.number == previous_tx:
                    raise DiagnosticsError(
                        "E_LOG_DUPLICATE",
                        location=f"{location}:tx_id",
                        details={"tx_id": tx.canonical},
                    )
                if tx.number < previous_tx:
                    raise DiagnosticsError(
                        "E_LOG_ORDER",
                        location=f"{location}:tx_id",
                        details={"previous": previous_tx, "current": tx.number},
                    )
                if tx.number != previous_tx + 1:
                    raise DiagnosticsError(
                        "E_LOG_GAP",
                        location=f"{location}:tx_id",
                        details={"previous": previous_tx, "current": tx.number},
                    )
            previous_tx = tx.number

            payload_bytes = require_int(
                raw.get("payload_bytes"),
                code="E_LOG_BYTES",
                location=f"{location}:payload_bytes",
                minimum=1,
            )
            applied = require_bool(
                raw.get("applied", raw.get("replayed")),
                code="E_LOG_COMMIT",
                location=f"{location}:applied",
            )
            if saw_unapplied and applied:
                raise DiagnosticsError(
                    "E_LOG_COMMIT",
                    location=f"{location}:applied",
                    details={"reason": "applied records must form a prefix"},
                )
            if not applied:
                saw_unapplied = True
            replay_time = raw.get("replay_time_ms")
            if replay_time is not None:
                replay_time = require_int(
                    replay_time,
                    code="E_V2_TIMESTAMP",
                    location=f"{location}:replay_time_ms",
                    minimum=0,
                )
            digest_source.append(
                [tx.canonical, payload_bytes, applied, replay_time, raw.get("seq")]
            )
            records.append(
                LogRecord(
                    seq=sequence,
                    tx_id=tx.canonical,
                    tx_num=tx.number,
                    payload_bytes=payload_bytes,
                    applied=applied,
                    replay_time_ms=replay_time,
                    location=location,
                )
            )

    if not records:
        raise DiagnosticsError("E_LOG_ORDER", message="replay log is empty", location=str(path))
    return records, sha256_bytes(stable_json_bytes(digest_source))
