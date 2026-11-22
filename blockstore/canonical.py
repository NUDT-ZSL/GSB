from __future__ import annotations

import base64
import binascii
import hashlib
import json
from pathlib import Path
from typing import Any

from .errors import BlockStoreError, ErrorCode


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def read_json_file(path: str | Path) -> Any:
    source = Path(path)
    try:
        with source.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError as exc:
        raise BlockStoreError(
            ErrorCode.INVALID_JSON,
            "JSON file does not exist",
            {"path": str(source)},
        ) from exc
    except json.JSONDecodeError as exc:
        raise BlockStoreError(
            ErrorCode.INVALID_JSON,
            "JSON file is malformed",
            {
                "path": str(source),
                "line": exc.lineno,
                "column": exc.colno,
                "reason": exc.msg,
            },
        ) from exc
    except OSError as exc:
        raise BlockStoreError(
            ErrorCode.IO_ERROR,
            "unable to read JSON file",
            {"path": str(source), "reason": str(exc)},
        ) from exc


def _decode_with_encoding(text: str, encoding: str) -> bytes:
    normalized = encoding.lower()
    if normalized in {"utf8", "utf-8", "text"}:
        return text.encode("utf-8")
    if normalized == "hex":
        try:
            return bytes.fromhex(text)
        except ValueError as exc:
            raise ValueError("invalid hex data") from exc
    if normalized in {"base64", "b64"}:
        try:
            return base64.b64decode(text, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("invalid base64 data") from exc
    raise ValueError(f"unsupported encoding {encoding!r}")


def decode_block_data(entry: dict[str, Any], block_size: int, context: str) -> bytes:
    if "data" in entry:
        text = entry["data"]
    elif "content" in entry:
        text = entry["content"]
    else:
        raise BlockStoreError(
            ErrorCode.INVALID_BLOCK_DATA,
            "block data is missing",
            {"context": context},
        )
    if not isinstance(text, str):
        raise BlockStoreError(
            ErrorCode.INVALID_BLOCK_DATA,
            "block data must be a string",
            {"context": context},
        )
    encoding = entry.get("encoding", "utf8")
    if not isinstance(encoding, str):
        raise BlockStoreError(
            ErrorCode.INVALID_BLOCK_DATA,
            "block encoding must be a string",
            {"context": context},
        )
    try:
        data = _decode_with_encoding(text, encoding)
    except ValueError as exc:
        raise BlockStoreError(
            ErrorCode.INVALID_BLOCK_DATA,
            str(exc),
            {"context": context, "encoding": encoding},
        ) from exc
    if len(data) != block_size:
        raise BlockStoreError(
            ErrorCode.INVALID_BLOCK_DATA,
            "decoded block length does not equal volume block size",
            {
                "context": context,
                "expected_length": block_size,
                "actual_length": len(data),
                "encoding": encoding,
            },
        )
    return data
