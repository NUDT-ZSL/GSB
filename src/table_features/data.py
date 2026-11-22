"""Deterministic local JSONL sample generation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np

from .canonical import atomic_write_bytes, canonical_bytes, sha256_bytes


DEFAULT_ID_COLUMN = "row_id"
DEFAULT_LABEL_COLUMN = "label"


def generate_rows(seed: int, n_rows: int = 24) -> list[dict[str, Any]]:
    """Generate a small, deterministic supervised tabular sample."""

    if n_rows < 1:
        raise ValueError("n_rows must be positive")
    rng = np.random.default_rng(seed)
    colors = np.array(["red", "blue", "green", "amber"])
    regions = np.array(["north", "south", "east", "west"])
    channels = np.array(["web", "store", "partner"])

    color_idx = rng.integers(0, len(colors), size=n_rows)
    region_idx = rng.integers(0, len(regions), size=n_rows)
    channel_idx = rng.integers(0, len(channels), size=n_rows)
    age = rng.integers(18, 71, size=n_rows)
    income = np.round(rng.lognormal(mean=10.5, sigma=0.42, size=n_rows), 2)
    tenure = rng.integers(0, 121, size=n_rows)

    logit = (
        -0.35
        + 0.018 * (age - 42)
        + 0.00008 * (income - 35000.0)
        + 0.004 * (tenure - 55)
        + np.where(colors[color_idx] == "red", 0.55, 0.0)
        + np.where(regions[region_idx] == "west", 0.35, 0.0)
        + np.where(channels[channel_idx] == "partner", -0.25, 0.0)
    )
    probability = 1.0 / (1.0 + np.exp(-logit))
    labels = (rng.random(n_rows) < probability).astype(int)

    rows: list[dict[str, Any]] = []
    for index in range(n_rows):
        rows.append(
            {
                "row_id": f"row-{index + 1:04d}",
                "age": int(age[index]),
                "income": float(income[index]),
                "tenure": int(tenure[index]),
                "color": str(colors[color_idx[index]]),
                "region": str(regions[region_idx[index]]),
                "channel": str(channels[channel_idx[index]]),
                "label": int(labels[index]),
            }
        )
    return rows


def jsonl_bytes(rows: Iterable[dict[str, Any]]) -> bytes:
    return b"".join(canonical_bytes(row) + b"\n" for row in rows)


def write_jsonl(path: str | Path, rows: Iterable[dict[str, Any]]) -> str:
    data = jsonl_bytes(rows)
    atomic_write_bytes(path, data)
    return sha256_bytes(data)


def load_jsonl(path: str | Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with Path(path).open("rb") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSONL at line {line_number}: {exc}") from exc
            if not isinstance(value, dict):
                raise ValueError(f"JSONL line {line_number} is not an object")
            rows.append(value)
    return rows


def sample_content_digest(
    rows: Sequence[dict[str, Any]],
    *,
    id_column: str = DEFAULT_ID_COLUMN,
    columns: Sequence[str] | None = None,
) -> str:
    """Hash a row-order-preserving projection of the sample.

    Feature-specific projections let an unrelated column change without
    invalidating every cached feature. The identity column is always included
    so fold assignment and row order are represented.
    """

    selected = list(dict.fromkeys([id_column, *(columns or [])]))
    projection = [[row.get(column) for column in selected] for row in rows]
    return sha256_bytes(canonical_bytes({"columns": selected, "values": projection}))


def row_map(rows: Sequence[dict[str, Any]], id_column: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        row_id = str(row[id_column])
        if row_id in result:
            raise ValueError(f"duplicate row_id: {row_id}")
        result[row_id] = row
    return result

