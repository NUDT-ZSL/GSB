"""Deterministic JSON-lines logging for anomaly and demo evidence."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from .util import atomic_write_text, canonical_json


class JsonlLogger:
    def __init__(self, path: str | Path | None) -> None:
        self.path = None if path is None else Path(path)
        self.events: list[dict[str, Any]] = []

    def emit(self, event: str, **fields: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {"event": event}
        for key in sorted(fields):
            payload[key] = fields[key]
        self.events.append(payload)
        return payload

    def emit_error(self, event: str, error: Mapping[str, Any], **fields: Any) -> dict[str, Any]:
        payload_fields = dict(fields)
        payload_fields["code"] = error.get("code")
        payload_fields["error"] = dict(error)
        return self.emit(event, **payload_fields)

    def text(self) -> str:
        if not self.events:
            return ""
        return "".join(canonical_json(event) + "\n" for event in self.events)

    def flush(self) -> None:
        if self.path is not None:
            atomic_write_text(self.path, self.text())
