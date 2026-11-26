from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

from .storage import nvme_file_lock
from .util import sha256_bytes


MAGIC = b"KVSWAP01"
FOOTER_SIZE = 32


def floor_convert(value: int, scale: int, carry_in: int = 0) -> Dict[str, int | str]:
    if scale <= 0:
        raise ValueError("scale must be positive")
    total = int(value) + int(carry_in)
    fixed_delta, carry_out = divmod(total, scale)
    return {
        "fixed_delta": int(fixed_delta),
        "carry_out": int(carry_out),
        "discarded_remainder": int(carry_out),
        "error_bound_bytes": int(scale - 1),
        "rounding_direction": "floor",
    }


def add_conversion(
    conn: Any,
    *,
    scope_type: str,
    scope_key: str,
    source_value_text: str,
    scale: int,
    clock_ms: int,
    value: int = 0,
    carry_in: int = 0,
    seq_id: Optional[str] = None,
    layer_id: Optional[str] = None,
    detail: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    result = floor_convert(value, scale, carry_in)
    cursor = conn.execute(
        """
        INSERT INTO ledger_conversions(
            scope_type, scope_key, seq_id, layer_id, source_value_text, scale,
            rounding_direction, carry_in, fixed_delta, carry_out,
            discarded_remainder, error_bound_bytes, clock_ms, detail_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            scope_type,
            scope_key,
            seq_id,
            layer_id,
            source_value_text,
            scale,
            result["rounding_direction"],
            carry_in,
            result["fixed_delta"],
            result["carry_out"],
            result["discarded_remainder"],
            result["error_bound_bytes"],
            clock_ms,
            json.dumps(detail or {}, sort_keys=True, default=str),
        ),
    )
    result["conversion_id"] = cursor.lastrowid
    return result


def encode_frame(header: Dict[str, Any], payload: bytes) -> bytes:
    header_bytes = json.dumps(header, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    prefix = MAGIC + len(header_bytes).to_bytes(4, "big") + header_bytes + payload
    return prefix + bytes.fromhex(sha256_bytes(prefix))


def frame_length(header_bytes_len: int, payload_size: int) -> int:
    return len(MAGIC) + 4 + header_bytes_len + payload_size + FOOTER_SIZE


def expected_frame_length(header: Dict[str, Any]) -> int:
    header_bytes = json.dumps(header, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return frame_length(len(header_bytes), int(header["actual_bytes"]))


def read_frame(path: str | Path, offset: int, expected_length: Optional[int] = None) -> Dict[str, Any]:
    path = Path(path)
    if not path.exists():
        return {"valid": False, "reason": "file_missing"}
    with path.open("rb") as handle:
        handle.seek(offset)
        magic = handle.read(len(MAGIC))
        if magic != MAGIC:
            return {"valid": False, "reason": "bad_magic"}
        length_bytes = handle.read(4)
        if len(length_bytes) != 4:
            return {"valid": False, "reason": "short_header_length"}
        header_len = int.from_bytes(length_bytes, "big")
        header_bytes = handle.read(header_len)
        if len(header_bytes) != header_len:
            return {"valid": False, "reason": "short_header"}
        try:
            header = json.loads(header_bytes.decode("utf-8"))
        except Exception:
            return {"valid": False, "reason": "bad_header_json"}
        payload_size = int(header.get("actual_bytes", -1))
        if payload_size < 0:
            return {"valid": False, "reason": "bad_payload_size"}
        payload = handle.read(payload_size)
        if len(payload) != payload_size:
            return {"valid": False, "reason": "short_payload"}
        footer = handle.read(FOOTER_SIZE)
        if len(footer) != FOOTER_SIZE:
            return {"valid": False, "reason": "short_footer"}
        prefix = MAGIC + length_bytes + header_bytes + payload
        if footer != bytes.fromhex(sha256_bytes(prefix)):
            return {"valid": False, "reason": "digest_mismatch", "header": header}
        payload_digest = sha256_bytes(payload)
        if header.get("payload_digest") != payload_digest:
            return {"valid": False, "reason": "payload_digest_mismatch", "header": header}
        frame_len = len(prefix) + FOOTER_SIZE
        if expected_length is not None and frame_len != expected_length:
            return {"valid": False, "reason": "length_mismatch", "header": header}
        return {
            "valid": True,
            "header": header,
            "payload": payload,
            "frame_length": frame_len,
            "payload_digest": payload_digest,
        }


def _pwrite_frame(path: str | Path, offset: int, frame: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT
    fd = os.open(str(path), flags, 0o600)
    try:
        written = 0
        view = memoryview(frame)
        while written < len(frame):
            written += os.pwrite(fd, view[written:], offset + written)
        os.fsync(fd)
    finally:
        os.close(fd)


def write_frame_at(
    path: str | Path,
    lock_path: str | Path,
    offset: int,
    frame: bytes,
    *,
    already_locked: bool = False,
) -> None:
    if already_locked:
        _pwrite_frame(path, offset, frame)
        return
    with nvme_file_lock(lock_path):
        _pwrite_frame(path, offset, frame)
