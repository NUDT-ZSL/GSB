from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO


def canonical_json(value: Any) -> str:
    """Canonical JSON used for hashes, keys and deterministic comparisons."""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def stable_hash(value: Any) -> str:
    return sha256_hex(canonical_json(value))


def utc_now_text() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def monotonic_ms() -> int:
    return time.monotonic_ns() // 1_000_000


def read_json_file(path: str | os.PathLike[str]) -> Any:
    file_path = Path(path)
    try:
        with file_path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError as exc:
        from .errors import PolicyError

        raise PolicyError(
            f"file not found: {file_path}",
            location=str(file_path),
        ) from exc
    except json.JSONDecodeError as exc:
        from .errors import PolicyError

        raise PolicyError(
            f"invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}",
            location=f"{file_path}:{exc.lineno}:{exc.colno}",
        ) from exc


class JsonLogger:
    """Small structured JSON-lines logger shared by the demo processes."""

    def __init__(
        self,
        path: str | os.PathLike[str] | None,
        component: str,
        stream: TextIO | None = None,
    ) -> None:
        self.component = component
        self.stream = stream
        self.path = Path(path) if path else None
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, level: str, event: str, **fields: Any) -> dict[str, Any]:
        record = {
            "ts": utc_now_text(),
            "level": level,
            "component": self.component,
            "event": event,
        }
        record.update(fields)
        line = canonical_json(record) + "\n"
        if self.path:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()
        if self.stream is not None:
            self.stream.write(line)
            self.stream.flush()
        return record

    def info(self, event: str, **fields: Any) -> dict[str, Any]:
        return self.log("INFO", event, **fields)

    def warning(self, event: str, **fields: Any) -> dict[str, Any]:
        return self.log("WARNING", event, **fields)

    def error(self, event: str, **fields: Any) -> dict[str, Any]:
        return self.log("ERROR", event, **fields)


def emit_json_line(payload: dict[str, Any], stream: TextIO | None = None) -> None:
    target = stream or sys.stdout
    target.write(canonical_json(payload) + "\n")
    target.flush()

