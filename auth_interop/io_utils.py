from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def json_dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def parse_json_text(text: str, source: str = "<json>") -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{source}: invalid JSON at line {exc.lineno} column {exc.colno}") from exc


def read_json_file(path: str | Path) -> Any:
    p = Path(path)
    with p.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def atomic_write_json(path: str | Path, payload: Any) -> Path:
    """Write and parse-check a temporary file, then atomically replace target."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    encoded = json_dumps(payload).encode("utf-8")

    # Validate the exact bytes before publishing.
    json.loads(encoded.decode("utf-8"))

    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=str(target.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())

        # Re-read before rename so a truncated temp file can never be published.
        with tmp_path.open("rb") as verify_handle:
            json.loads(verify_handle.read().decode("utf-8"))

        os.replace(tmp_path, target)
        _fsync_directory(target.parent)
        return target
    except BaseException:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _fsync_directory(directory: Path) -> None:
    if not hasattr(os, "O_DIRECTORY"):
        return
    try:
        fd = os.open(str(directory), os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def cleanup_temp_files(directory: str | Path) -> list[str]:
    """Remove atomic-writer leftovers so a killed process cannot leave decoys."""

    root = Path(directory)
    if not root.exists():
        return []
    removed: list[str] = []
    patterns = (".*.tmp", "*.tmp")
    for pattern in patterns:
        for path in root.glob(pattern):
            if not path.is_file():
                continue
            try:
                path.unlink()
                removed.append(str(path))
            except OSError:
                pass
    return removed


def sha256_bytes(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | Path) -> str:
    p = Path(path)
    return sha256_bytes(p.read_bytes())
