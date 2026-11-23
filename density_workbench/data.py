from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import numpy as np

from .canonical import sha256_obj, write_json_atomic
from .config import WorkbenchConfig
from .errors import (
    E_DUPLICATE_SAMPLE_ID,
    E_FEATURE_DIM,
    E_INPUT_INVALID,
    E_SAMPLE_NOT_FOUND,
    BusinessError,
)


def load_samples(path: str | Path) -> list[dict[str, Any]]:
    source = Path(path)
    if not source.exists():
        raise BusinessError(
            code=E_INPUT_INVALID,
            message=f"sample file does not exist: {source}",
            details={"path": str(source)},
        )
    records: list[dict[str, Any]] = []
    with source.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise BusinessError(
                    code=E_INPUT_INVALID,
                    message=f"invalid JSON on line {line_number}: {exc}",
                    details={"line": line_number, "path": str(source)},
                ) from exc
            records.append(record)
    validate_samples(records)
    return records


def validate_samples(records: list[dict[str, Any]]) -> None:
    if not isinstance(records, list) or not records:
        raise BusinessError(
            code=E_INPUT_INVALID,
            message="at least one sample is required",
            details={"field": "samples"},
        )
    seen: set[str] = set()
    dim: int | None = None
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise BusinessError(
                code=E_INPUT_INVALID,
                message=f"sample at index {index} must be an object",
                details={"index": index, "field": "sample"},
            )
        sample_id = record.get("sample_id")
        if not isinstance(sample_id, str) or not sample_id:
            raise BusinessError(
                code=E_INPUT_INVALID,
                message=f"sample at index {index} has invalid sample_id",
                details={"index": index, "field": "sample_id"},
            )
        if sample_id in seen:
            raise BusinessError(
                code=E_DUPLICATE_SAMPLE_ID,
                message=f"duplicate sample_id: {sample_id}",
                details={"sample_id": sample_id, "index": index},
            )
        seen.add(sample_id)
        feature = record.get("feature")
        if not isinstance(feature, (list, tuple)):
            raise BusinessError(
                code=E_FEATURE_DIM,
                message=f"sample {sample_id} feature must be a numeric vector",
                details={"sample_id": sample_id, "field": "feature"},
            )
        current_dim = len(feature)
        if current_dim < 2 or current_dim > 8:
            raise BusinessError(
                code=E_FEATURE_DIM,
                message=(
                    f"sample {sample_id} feature dimension {current_dim} "
                    "is outside the supported range 2..8"
                ),
                details={
                    "sample_id": sample_id,
                    "dimension": current_dim,
                    "allowed": [2, 8],
                },
            )
        if dim is None:
            dim = current_dim
        elif current_dim != dim:
            raise BusinessError(
                code=E_FEATURE_DIM,
                message=(
                    f"sample {sample_id} feature dimension {current_dim} "
                    f"does not match dataset dimension {dim}"
                ),
                details={
                    "sample_id": sample_id,
                    "dimension": current_dim,
                    "expected_dimension": dim,
                },
            )
        for position, value in enumerate(feature):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
            ):
                raise BusinessError(
                    code=E_INPUT_INVALID,
                    message=f"sample {sample_id} has a non-finite feature value",
                    details={"sample_id": sample_id, "position": position},
                )
        group = record.get("group", "")
        if not isinstance(group, str):
            raise BusinessError(
                code=E_INPUT_INVALID,
                message=f"sample {sample_id} group must be a string",
                details={"sample_id": sample_id, "field": "group"},
            )


def samples_to_matrix(
    records: list[dict[str, Any]],
) -> tuple[list[str], list[str], np.ndarray]:
    sample_ids = [str(record["sample_id"]) for record in records]
    groups = [str(record.get("group", "")) for record in records]
    features = np.asarray([record["feature"] for record in records], dtype=np.float64)
    if features.ndim != 2:
        raise BusinessError(
            code=E_FEATURE_DIM,
            message="sample matrix must be two-dimensional",
            details={"shape": list(features.shape)},
        )
    return sample_ids, groups, features


def dataset_hash(records: list[dict[str, Any]]) -> str:
    return sha256_obj(
        [
            {
                "sample_id": record["sample_id"],
                "feature": record["feature"],
                "group": record.get("group", ""),
            }
            for record in records
        ]
    )


def sample_chunks(
    sample_ids: list[str], chunk_size: int
) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    for chunk_id, start in enumerate(range(0, len(sample_ids), chunk_size)):
        ids = sample_ids[start : start + chunk_size]
        chunks.append(
            {
                "chunk_id": chunk_id,
                "start": start,
                "stop": start + len(ids),
                "sample_ids": list(ids),
                "sample_hash": sha256_obj(ids),
            }
        )
    return chunks


def sample_index(sample_ids: list[str], target: str) -> int:
    try:
        return sample_ids.index(target)
    except ValueError as exc:
        raise BusinessError(
            code=E_SAMPLE_NOT_FOUND,
            message=f"sample_id not found: {target}",
            details={"sample_id": target},
        ) from exc


def deterministic_samples(
    *,
    n_samples: int = 48,
    dimensions: int = 3,
    n_groups: int = 4,
    seed: int = 20260101,
) -> list[dict[str, Any]]:
    if n_samples < n_groups:
        raise ValueError("n_samples must be at least n_groups")
    if dimensions < 2 or dimensions > 8:
        raise ValueError("dimensions must be in 2..8")
    rng = np.random.default_rng(seed)
    centers = rng.normal(0.0, 2.4, size=(n_groups, dimensions))
    scales = rng.uniform(0.35, 0.95, size=(n_groups, dimensions))
    labels = np.arange(n_samples) % n_groups
    rng.shuffle(labels)
    records: list[dict[str, Any]] = []
    for index in range(n_samples):
        group_index = int(labels[index])
        feature = centers[group_index] + rng.normal(
            0.0, scales[group_index], size=dimensions
        )
        records.append(
            {
                "sample_id": f"sample-{index:04d}",
                "feature": [round(float(value), 8) for value in feature],
                "group": f"group-{group_index}",
            }
        )
    return records


def write_samples(path: str | Path, records: list[dict[str, Any]]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.tmp-{id(records)}")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(
                json.dumps(
                    {
                        "sample_id": record["sample_id"],
                        "feature": record["feature"],
                        "group": record.get("group", ""),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
                + "\n"
            )
    tmp.replace(target)


def ensure_workspace(
    root: str | Path,
    *,
    force: bool = False,
    n_samples: int = 48,
    dimensions: int = 3,
) -> dict[str, Path]:
    base = Path(root).resolve()
    base.mkdir(parents=True, exist_ok=True)
    samples_path = base / "samples.jsonl"
    config_path = base / "config.json"
    cache_dir = base / "cache"
    snapshot_dir = base / "snapshots"
    artifact_dir = base / "artifacts"
    for directory in (cache_dir, snapshot_dir, artifact_dir):
        directory.mkdir(parents=True, exist_ok=True)
    if force or not samples_path.exists():
        write_samples(
            samples_path,
            deterministic_samples(
                n_samples=n_samples,
                dimensions=dimensions,
                seed=20260101,
            ),
        )
    if force or not config_path.exists():
        config = WorkbenchConfig()
        write_json_atomic(config_path, config.to_dict(), indent=2)
    return {
        "root": base,
        "samples": samples_path,
        "config": config_path,
        "cache_dir": cache_dir,
        "snapshot_dir": snapshot_dir,
        "artifact_dir": artifact_dir,
    }
