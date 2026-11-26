from __future__ import annotations

import hashlib
import json
from typing import Any, List, Tuple


def canonical_dumps(value: Any, *, indent: int | None = None) -> str:
    if indent is None:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        indent=indent,
        separators=(",", ": "),
        allow_nan=False,
    ) + "\n"


def canonical_bytes(value: Any) -> bytes:
    return canonical_dumps(value).encode("utf-8")


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stable_diff(expected: Any, actual: Any, path: str = "$") -> List[dict]:
    diffs: List[dict] = []
    if type(expected) is not type(actual):
        diffs.append(
            {
                "path": path,
                "kind": "type",
                "expected_type": type(expected).__name__,
                "actual_type": type(actual).__name__,
                "expected": expected,
                "actual": actual,
            }
        )
        return diffs
    if isinstance(expected, dict):
        keys = sorted(set(expected) | set(actual))
        for key in keys:
            child = f"{path}.{key}"
            if key not in expected:
                diffs.append({"path": child, "kind": "unexpected", "actual": actual[key]})
            elif key not in actual:
                diffs.append({"path": child, "kind": "missing", "expected": expected[key]})
            else:
                diffs.extend(stable_diff(expected[key], actual[key], child))
    elif isinstance(expected, list):
        if len(expected) != len(actual):
            diffs.append(
                {
                    "path": path,
                    "kind": "length",
                    "expected_length": len(expected),
                    "actual_length": len(actual),
                }
            )
        for index, (left, right) in enumerate(zip(expected, actual)):
            diffs.extend(stable_diff(left, right, f"{path}[{index}]"))
    elif expected != actual:
        diffs.append(
            {
                "path": path,
                "kind": "value",
                "expected": expected,
                "actual": actual,
            }
        )
    return diffs


def sorted_entries(items: List[Tuple[str, Any]]) -> List[Tuple[str, Any]]:
    return sorted(items, key=lambda item: item[0])
