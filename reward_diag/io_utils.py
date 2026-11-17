from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def pretty_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        indent=2,
    ) + "\n"


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_obj(value: Any) -> str:
    return sha256_text(canonical_json(value))


def sha256_file(path: str | os.PathLike[str]) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: str | os.PathLike[str]) -> Any:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def ensure_dir(path: str | os.PathLike[str]) -> Path:
    target = Path(path)
    target.mkdir(parents=True, exist_ok=True)
    return target


def _fsync_dir(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def atomic_write_text(path: str | os.PathLike[str], text: str) -> None:
    target = Path(path)
    ensure_dir(target.parent)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=str(target.parent),
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        _fsync_dir(target.parent)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def atomic_write_json(path: str | os.PathLike[str], value: Any) -> None:
    atomic_write_text(path, pretty_json(value))


def remove_tree(path: str | os.PathLike[str]) -> None:
    target = Path(path)
    if target.exists():
        shutil.rmtree(target)


def move_directory_atomic(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
    source_path = Path(source)
    target_path = Path(target)
    ensure_dir(target_path.parent)
    os.replace(source_path, target_path)
    _fsync_dir(target_path.parent)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def require_ascii_paths(paths: list[str | os.PathLike[str]]) -> None:
    for item in paths:
        text = os.fspath(item)
        if any(ord(character) > 127 for character in text):
            raise ValueError(f"E_NON_ASCII_PATH: {text!r}")


def stable_task_key(pair_id: str, stage: str) -> str:
    return hashlib.sha256(f"{pair_id}|{stage}".encode("utf-8")).hexdigest()


def sorted_failures(failures: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def key(item: dict[str, Any]) -> tuple[str, str, str, str, str, str]:
        return (
            str(item.get("pair_id") or "\uffff"),
            str(item.get("batch_id") or ""),
            str(item.get("stage") or ""),
            str(item.get("business_code") or ""),
            str(item.get("exception_type") or ""),
            str(item.get("source") or ""),
        )

    return sorted(failures, key=key)
