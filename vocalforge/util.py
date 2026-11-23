"""Deterministic serialization, hashing, and numeric helpers."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Iterable

import numpy as np


def to_jsonable(value: Any) -> Any:
    """Convert numpy and nested values into JSON-safe Python objects."""
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(v) for v in value]
    if isinstance(value, np.ndarray):
        return [to_jsonable(v) for v in value.tolist()]
    if isinstance(value, np.generic):
        return to_jsonable(value.item())
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        return value
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(
        to_jsonable(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _canonical_for_digest(value: Any) -> Any:
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return _canonical_for_digest(value.to_dict())
    if is_dataclass(value) and not isinstance(value, type):
        return _canonical_for_digest(asdict(value))
    if isinstance(value, dict):
        return {str(k): _canonical_for_digest(v) for k, v in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_canonical_for_digest(v) for v in value]
    if isinstance(value, np.ndarray):
        return [_canonical_for_digest(v) for v in value.tolist()]
    if isinstance(value, np.generic):
        return _canonical_for_digest(value.item())
    if isinstance(value, float):
        if not math.isfinite(value):
            return "nan"
        if value == 0.0:
            return "0x0.0p+0"
        return value.hex()
    return value


def digest_payload(value: Any) -> str:
    text = json.dumps(
        _canonical_for_digest(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def stable_round(value: float, decimals: int = 12) -> float:
    if not math.isfinite(value):
        return 0.0
    rounded = round(float(value), decimals)
    return 0.0 if rounded == 0.0 else rounded


def quantile(values: Iterable[float], probability: float) -> float:
    """Linear-interpolation quantile with deterministic ordering."""
    arr = np.sort(np.asarray(list(values), dtype=np.float64))
    if arr.size == 0:
        return 0.0
    if arr.size == 1:
        return float(arr[0])
    position = (arr.size - 1) * float(probability)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return float(arr[lower])
    fraction = position - lower
    return float(arr[lower] * (1.0 - fraction) + arr[upper] * fraction)


def quantile_summary(values: Iterable[float]) -> dict[str, float]:
    vals = [float(v) for v in values]
    if not vals:
        zeros = {k: 0.0 for k in ("p50", "p90", "p95", "p99", "max", "mean")}
        return zeros
    return {
        "p50": stable_round(quantile(vals, 0.50)),
        "p90": stable_round(quantile(vals, 0.90)),
        "p95": stable_round(quantile(vals, 0.95)),
        "p99": stable_round(quantile(vals, 0.99)),
        "max": stable_round(max(vals)),
        "mean": stable_round(sum(vals) / len(vals)),
    }


def frame_intervals(frame_indices: Iterable[int]) -> list[list[int]]:
    frames = sorted(set(int(v) for v in frame_indices))
    if not frames:
        return []
    intervals: list[list[int]] = []
    start = previous = frames[0]
    for frame in frames[1:]:
        if frame == previous + 1:
            previous = frame
            continue
        intervals.append([start, previous])
        start = previous = frame
    intervals.append([start, previous])
    return intervals


def ensure_parent(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def atomic_write_text(path: Path, text: str) -> None:
    ensure_parent(path)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(text, encoding="utf-8")
    temp.replace(path)


def jsonl_dumps(rows: Iterable[dict[str, Any]]) -> str:
    return "\n".join(canonical_json(row) for row in rows) + "\n"
