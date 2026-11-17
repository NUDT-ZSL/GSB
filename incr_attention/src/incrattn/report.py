"""Atomic JSON report writer with deterministic block ordering."""

from __future__ import annotations

import json
import os
import tempfile

from .util import canonical_json


def _sort_block_lists(payload):
    if isinstance(payload, dict):
        result = {}
        for key, value in payload.items():
            if (
                isinstance(value, list)
                and value
                and isinstance(value[0], dict)
                and "block_index" in value[0]
            ):
                value = sorted(value, key=lambda item: item["block_index"])
            result[key] = _sort_block_lists(value)
        return result
    if isinstance(payload, list):
        return [_sort_block_lists(item) for item in payload]
    return payload


def write_report_atomic(path: str, payload: dict) -> str:
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    ordered = _sort_block_lists(payload)
    text = json.dumps(ordered, indent=2, sort_keys=True, ensure_ascii=False)
    fd, tmp_path = tempfile.mkstemp(prefix=".report-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise
    return path


def report_digest(payload: dict) -> str:
    import hashlib

    return hashlib.sha256(
        canonical_json(_sort_block_lists(payload)).encode("utf-8")
    ).hexdigest()
