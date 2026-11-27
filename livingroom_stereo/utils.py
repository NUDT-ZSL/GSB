from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable


def canonical_float(value: float, ndigits: int = 9) -> float:
    """Round a finite float and normalise negative zero."""

    result = round(float(value), ndigits)
    if not math.isfinite(result):
        raise ValueError(f"non-finite result cannot be serialized: {value!r}")
    if result == 0.0:
        return 0.0
    return result


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        allow_nan=False,
    ) + "\n"


def compact_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def fingerprint_json(value: Any) -> str:
    return hashlib.sha256(compact_json(value).encode("utf-8")).hexdigest()


def write_json_atomic(path: str | os.PathLike[str], value: Any) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=str(target.parent),
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(canonical_json(value))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def read_json(path: str | os.PathLike[str]) -> Any:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def deep_diff(
    before: Any,
    after: Any,
    *,
    path: str = "$",
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Return deterministic leaf-level differences between JSON values."""

    differences: list[dict[str, Any]] = []

    def visit(left: Any, right: Any, current: str) -> None:
        if len(differences) >= limit:
            return
        if type(left) is not type(right):
            differences.append(
                {"path": current, "before": left, "after": right}
            )
            return
        if isinstance(left, dict):
            left_keys = set(left)
            right_keys = set(right)
            for key in sorted(left_keys | right_keys):
                child = f"{current}.{key}"
                if key not in left:
                    differences.append(
                        {"path": child, "before": None, "after": right[key]}
                    )
                elif key not in right:
                    differences.append(
                        {"path": child, "before": left[key], "after": None}
                    )
                else:
                    visit(left[key], right[key], child)
                if len(differences) >= limit:
                    return
            return
        if isinstance(left, list):
            if len(left) != len(right):
                differences.append(
                    {
                        "path": f"{current}.length",
                        "before": len(left),
                        "after": len(right),
                    }
                )
            for index, (left_item, right_item) in enumerate(
                zip(left, right)
            ):
                visit(left_item, right_item, f"{current}[{index}]")
                if len(differences) >= limit:
                    return
            return
        if left != right:
            differences.append(
                {"path": current, "before": left, "after": right}
            )

    visit(before, after, path)
    return differences


def percentile_nearest_rank(values: Iterable[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("percentile requires at least one value")
    if not 0.0 <= fraction <= 1.0:
        raise ValueError("fraction must be between 0 and 1")
    index = max(0, min(len(ordered) - 1, math.ceil(fraction * len(ordered)) - 1))
    return ordered[index]


def safe_version_name(version: str) -> str:
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
    if not version or any(character not in allowed for character in version):
        raise ValueError(
            "version may contain only letters, digits, '.', '_' and '-'"
        )
    if version in {".", ".."}:
        raise ValueError("invalid version name")
    return version
