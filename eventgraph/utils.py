from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def canonical_bytes(value: Any) -> bytes:
    return canonical_json(value).encode("utf-8")


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def text_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def read_json(path: str | Path) -> Any:
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def atomic_write_json(path: str | Path, value: Any) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    data = canonical_json(value) + "\n"
    fd, temporary = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=str(target.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def atomic_write_text(path: str | Path, value: str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=str(target.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def round_score(value: float) -> float:
    return round(float(value) + 0.0, 6)


def event_key(doc_index: int, local_id: str) -> str:
    return f"{doc_index:04d}:{local_id}"


def pair_key(event_a: str, event_b: str) -> str:
    if event_a <= event_b:
        return f"{event_a}|{event_b}"
    return f"{event_b}|{event_a}"


def event_sort_tuple(event: dict[str, Any]) -> tuple[int, int, str]:
    return (
        int(event["doc_index"]),
        int(event["order_start"]),
        str(event["local_id"]),
    )


def pair_sort_tuple(pair: dict[str, Any], events: dict[str, dict[str, Any]]) -> tuple:
    left = events[pair["left_event"]]
    right = events[pair["right_event"]]
    first, second = sorted(
        [event_sort_tuple(left), event_sort_tuple(right)],
    )
    return (
        -float(pair["total"]),
        first[0],
        first[2],
        second[0],
        second[2],
    )
