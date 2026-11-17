"""JSON-lines wire protocol helpers shared by client and service."""

from __future__ import annotations

import json
from typing import Any

from .deterministic import canonical_json


MAX_LINE_BYTES = 4 * 1024 * 1024
PROTOCOL_VERSION = 1


def encode_message(payload: dict[str, Any]) -> bytes:
    return (canonical_json(payload) + "\n").encode("utf-8")


def decode_message(raw: bytes) -> dict[str, Any]:
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("protocol message must be a JSON object")
    return value


def error_response(code: str, message: str, *, field_path: str | None = None,
                   details: dict[str, Any] | None = None,
                   request_id: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"ok": False, "code": code, "message": message}
    if field_path is not None:
        payload["field_path"] = field_path
    if details:
        payload["details"] = details
    if request_id is not None:
        payload["request_id"] = request_id
    return payload

