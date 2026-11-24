from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Dict

from .errors import VerificationError


def ensure_writable_directory(path: Path) -> Path:
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    probe = path / f".write-probe-{os.getpid()}-{threading.get_ident()}"
    try:
        probe.write_bytes(b"ok")
        probe.unlink(missing_ok=True)
    except OSError as exc:
        raise VerificationError(
            f"directory is not writable: {path}",
            code="E_PARAM",
            details={"path": str(path), "error": str(exc)},
        ) from exc
    return path


def parse_and_validate_json_bytes(raw: bytes, *, required_keys=None) -> Dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VerificationError(
            "temporary JSON content is incomplete or unparsable",
            code="E_CHECKPOINT_CORRUPT",
            details={"error": str(exc)},
        ) from exc
    if required_keys:
        missing = [key for key in required_keys if key not in value]
        if missing:
            raise VerificationError(
                "temporary JSON content is missing required keys",
                code="E_CHECKPOINT_CORRUPT",
                details={"missing": missing},
            )
    return value


def atomic_write_json(
    path: Path,
    payload: Any,
    *,
    required_keys=None,
    indent: int = 2,
) -> Path:
    """Write, parse-back validate, then atomically rename a JSON file."""

    path = Path(path)
    ensure_writable_directory(path.parent)
    tmp_path = path.with_name(path.name + ".tmp")
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=indent)
    raw = (data + "\n").encode("utf-8")

    with open(tmp_path, "wb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())

    with open(tmp_path, "rb") as handle:
        parsed_raw = handle.read()
    parse_and_validate_json_bytes(parsed_raw, required_keys=required_keys)

    os.replace(tmp_path, path)
    try:
        directory_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError:
        # Some Windows filesystems do not support directory fsync; rename is atomic there.
        pass
    return path


def read_json_file(path: Path) -> Any:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)
