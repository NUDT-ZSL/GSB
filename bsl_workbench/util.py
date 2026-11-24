from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Mapping


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def stable_json_dumps(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        separators=(",", ": "),
    ) + "\n"


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def atomic_write_text(path: os.PathLike[str] | str, text: str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=target.name + ".",
        suffix=".tmp",
        dir=str(target.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
        _fsync_directory(target.parent)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def atomic_write_json(path: os.PathLike[str] | str, value: Any) -> None:
    atomic_write_text(path, stable_json_dumps(value))


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


def safe_file_id(path: os.PathLike[str] | str) -> str:
    resolved = str(Path(path).expanduser().resolve())
    return "F-" + sha256_hex(resolved.encode("utf-8"))[:16]


def escape_name(name: str) -> str:
    return name.replace("~", "~0").replace("/", "~1")


def unescape_name(name: str) -> str:
    out = []
    i = 0
    while i < len(name):
        ch = name[i]
        if ch == "~" and i + 1 < len(name):
            if name[i + 1] == "0":
                out.append("~")
                i += 2
                continue
            if name[i + 1] == "1":
                out.append("/")
                i += 2
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def join_key(parent_key: str, name: str) -> str:
    leaf = escape_name(name)
    if not parent_key:
        return "/" + leaf
    return parent_key.rstrip("/") + "/" + leaf


def leaf_name(key: str) -> str:
    if key in ("", "/"):
        return key
    return unescape_name(key.rsplit("/", 1)[-1])


def sanitize_identifier(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")
    return cleaned or "file"


def sorted_mapping_items(mapping: Mapping[str, Any]) -> list[tuple[str, Any]]:
    return sorted(mapping.items(), key=lambda item: item[0])
