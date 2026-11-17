"""Atomic, integrity-checked JSON/bytes persistence."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable

from .errors import ErrorCode, SLPError


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def make_envelope(kind: str, payload: Any) -> dict[str, Any]:
    digest = sha256_hex(canonical_json_bytes(payload))
    return {
        "envelope": "slp-integrity-v1",
        "kind": kind,
        "sha256": digest,
        "payload": payload,
    }


def verify_envelope(document: Any, *, expected_kind: str | None = None) -> Any:
    if not isinstance(document, dict) or document.get("envelope") != "slp-integrity-v1":
        raise SLPError(ErrorCode.E_CHECKPOINT, "not an slp-integrity-v1 envelope")
    if expected_kind is not None and document.get("kind") != expected_kind:
        raise SLPError(
            ErrorCode.E_CHECKPOINT,
            f"envelope kind {document.get('kind')!r} != {expected_kind!r}",
        )
    if "payload" not in document or "sha256" not in document:
        raise SLPError(ErrorCode.E_CHECKPOINT, "envelope is missing payload or sha256")
    actual = sha256_hex(canonical_json_bytes(document["payload"]))
    if actual != document["sha256"]:
        raise SLPError(
            ErrorCode.E_CHECKPOINT,
            f"envelope integrity check failed: stored={document['sha256']} actual={actual}",
        )
    return document["payload"]


def atomic_write_bytes(path: str | Path, data: bytes) -> None:
    """Write a temporary file, fsync, re-read/verify, then atomically replace."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=str(target.parent),
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if temp_path.read_bytes() != data:
            raise SLPError(
                ErrorCode.E_IO,
                f"temporary write verification failed for {target}",
            )
        os.replace(temp_path, target)
        try:
            directory_fd = os.open(target.parent, os.O_RDONLY)
        except OSError:
            directory_fd = None
        if directory_fd is not None:
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except Exception:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
        raise


def atomic_write_json(path: str | Path, payload: Any, *, kind: str) -> dict[str, Any]:
    envelope = make_envelope(kind, payload)
    data = canonical_json_bytes(envelope) + b"\n"
    verify_envelope(json.loads(data.decode("utf-8")), expected_kind=kind)
    atomic_write_bytes(path, data)
    # Verify the exact bytes that a crash-recovery reader would see.
    parsed = json.loads(Path(path).read_text(encoding="utf-8"))
    verify_envelope(parsed, expected_kind=kind)
    return envelope


def read_json_envelope(path: str | Path, *, kind: str | None = None) -> Any:
    target = Path(path)
    try:
        document = json.loads(target.read_text(encoding="utf-8"))
    except OSError as error:
        raise SLPError(ErrorCode.E_IO, f"cannot read {target}: {error}") from error
    except json.JSONDecodeError as error:
        raise SLPError(
            ErrorCode.E_CHECKPOINT,
            f"invalid JSON in {target}: {error}",
        ) from error
    return verify_envelope(document, expected_kind=kind)


def cleanup_temp_files(directories: Iterable[str | Path]) -> list[str]:
    removed: list[str] = []
    for directory in directories:
        target = Path(directory)
        target.mkdir(parents=True, exist_ok=True)
        for pattern in ("*.tmp", ".*.tmp"):
            for path in sorted(target.glob(pattern)):
                if path.is_file():
                    try:
                        path.unlink()
                        removed.append(str(path))
                    except FileNotFoundError:
                        pass
    return removed
