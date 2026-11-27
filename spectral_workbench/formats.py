"""Readers and normalizers for the three delivered spectral format versions."""

from __future__ import annotations

import csv
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from .utils import array_sha256, ensure_dir, write_json


@dataclass
class NormalizedSpectrum:
    sample_id: str
    material_family: str
    instrument_source: str
    modality: str
    is_rare: bool
    version: str
    grid_unit: str
    grid: np.ndarray
    token_values: np.ndarray
    mask: np.ndarray
    missing_indices: tuple[int, ...]
    normalization_log: dict[str, Any]


@dataclass
class NormalizedDataset:
    version: str
    samples: list[NormalizedSpectrum]
    token_length: int

    @property
    def sample_ids(self) -> list[str]:
        return [sample.sample_id for sample in self.samples]

    def stacked(self) -> tuple[np.ndarray, np.ndarray]:
        return (
            np.stack([sample.token_values for sample in self.samples]),
            np.stack([sample.mask for sample in self.samples]),
        )


def _read_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _parse_value(version: str, raw_value: str) -> tuple[float, bool]:
    text = raw_value.strip()
    if version == "A":
        if text.lower() in {"", "nan", "na", "null"}:
            return math.nan, True
        value = float(text)
        return value, math.isnan(value)
    if version == "B":
        if text.lower() in {"", "nan", "na", "null"}:
            return math.nan, True
        value = float(text)
        return value, value < 0.0
    if version == "C":
        if text == "":
            return math.nan, True
        value = float(text)
        return value, math.isnan(value)
    raise ValueError(f"unsupported format version: {version}")


def read_version_file(
    path: str | Path, version: str, expected_length: int
) -> tuple[np.ndarray, np.ndarray, tuple[int, ...]]:
    rows = _read_rows(Path(path))
    if len(rows) != expected_length:
        raise ValueError(
            f"{path}: expected {expected_length} token rows, found {len(rows)}"
        )
    rows.sort(key=lambda row: int(row["token_index"]))
    expected_indices = list(range(expected_length))
    observed_indices = [int(row["token_index"]) for row in rows]
    if observed_indices != expected_indices:
        raise ValueError(
            f"{path}: token_index must be contiguous from 0 to {expected_length - 1}"
        )
    grid = np.empty(expected_length, dtype=np.float64)
    values = np.full(expected_length, np.nan, dtype=np.float64)
    missing: list[int] = []
    for index, row in enumerate(rows):
        grid[index] = float(row["grid_value"])
        value, is_missing = _parse_value(version, row["value"])
        if is_missing:
            missing.append(index)
        else:
            values[index] = value
    if not np.isfinite(grid).all():
        raise ValueError(f"{path}: grid values must be finite")
    if values.size and not np.isfinite(values[~np.isnan(values)]).all():
        raise ValueError(f"{path}: valid values must be finite")
    return grid, values, tuple(missing)


def _trapezoid_area(values: np.ndarray, grid: np.ndarray) -> float:
    if values.size == 1:
        return abs(float(values[0]))
    if values.size < 2:
        return 0.0
    return abs(float(np.sum(0.5 * (values[:-1] + values[1:]) * np.diff(grid))))


def _peak_area_normalizer(values: np.ndarray, grid: np.ndarray) -> float:
    valid = ~np.isnan(values)
    runs: list[tuple[int, int]] = []
    start: int | None = None
    for index, is_valid in enumerate(valid):
        if is_valid and start is None:
            start = index
        if not is_valid and start is not None:
            runs.append((start, index))
            start = None
    if start is not None:
        runs.append((start, len(valid)))
    area = sum(_trapezoid_area(values[left:right], grid[left:right]) for left, right in runs)
    return area


def _canonicalize(
    pre_normalized: np.ndarray, mask: np.ndarray, float_decimals: int
) -> np.ndarray:
    canonical = np.zeros_like(pre_normalized, dtype=np.float64)
    valid = mask.astype(bool)
    if not valid.any():
        return canonical
    values = pre_normalized[valid]
    mean = float(np.mean(values))
    std = float(np.std(values))
    if not np.isfinite(std) or std < 1.0e-15:
        std = 1.0
    centered = (values - mean) / std
    norm = float(np.sqrt(np.sum(centered * centered)))
    if not np.isfinite(norm) or norm < 1.0e-15:
        norm = 1.0
    canonical_valid = np.round(centered / norm, decimals=float_decimals)
    canonical_valid[np.abs(canonical_valid) < 10.0 ** (-float_decimals)] = 0.0
    canonical[valid] = canonical_valid
    return canonical


def normalize_spectrum(
    *,
    sample_meta: dict[str, Any],
    version: str,
    grid_unit: str,
    grid: np.ndarray,
    raw_values: np.ndarray,
    missing_indices: Iterable[int],
    float_decimals: int = 12,
) -> NormalizedSpectrum:
    missing = tuple(sorted(int(index) for index in missing_indices))
    mask = np.ones(raw_values.shape[0], dtype=np.int8)
    if missing:
        mask[np.asarray(missing, dtype=np.int64)] = 0
    valid = mask.astype(bool)
    valid_values = raw_values[valid]
    if valid_values.size == 0:
        raise ValueError(f"{sample_meta['sample_id']} version {version} has no valid tokens")

    if version == "A":
        normalizer_name = "max_intensity"
        normalizer_value = float(np.max(np.abs(valid_values)))
    elif version == "B":
        normalizer_name = "peak_area"
        normalizer_value = _peak_area_normalizer(raw_values, grid)
    elif version == "C":
        normalizer_name = "quantile_q90"
        normalizer_value = float(np.quantile(valid_values, 0.90))
    else:
        raise ValueError(f"unsupported format version: {version}")

    if not np.isfinite(normalizer_value) or abs(normalizer_value) < 1.0e-15:
        normalizer_value = 1.0
    pre_normalized = np.zeros_like(raw_values, dtype=np.float64)
    pre_normalized[valid] = valid_values / normalizer_value
    token_values = _canonicalize(pre_normalized, mask, float_decimals)

    log = {
        "sample_id": sample_meta["sample_id"],
        "material_family": sample_meta["material_family"],
        "instrument_source": sample_meta["instrument_source"],
        "version": version,
        "grid_unit": grid_unit,
        "token_length": int(raw_values.shape[0]),
        "normalizer": normalizer_name,
        "normalizer_value": normalizer_value,
        "raw_valid_min": float(np.min(valid_values)),
        "raw_valid_max": float(np.max(valid_values)),
        "pre_valid_min": float(np.min(pre_normalized[valid])),
        "pre_valid_max": float(np.max(pre_normalized[valid])),
        "token_valid_min": float(np.min(token_values[valid])),
        "token_valid_max": float(np.max(token_values[valid])),
        "missing_token_count": len(missing),
        "missing_token_indices": list(missing),
        "canonicalization": "zscore_then_l2_with_mask",
        "float_decimals": int(float_decimals),
        "mask_sha256": array_sha256(mask),
        "token_values_sha256": array_sha256(token_values),
    }
    return NormalizedSpectrum(
        sample_id=str(sample_meta["sample_id"]),
        material_family=str(sample_meta["material_family"]),
        instrument_source=str(sample_meta["instrument_source"]),
        modality=str(sample_meta["modality"]),
        is_rare=bool(sample_meta["is_rare"]),
        version=version,
        grid_unit=grid_unit,
        grid=grid.astype(np.float64, copy=True),
        token_values=token_values,
        mask=mask,
        missing_indices=missing,
        normalization_log=log,
    )


def load_normalized_dataset(
    data_root: str | Path, version: str, config: dict[str, Any]
) -> NormalizedDataset:
    data_root = Path(data_root)
    with (data_root / "manifest.json").open("r", encoding="utf-8") as handle:
        root_manifest = json.load(handle)
    version_info = root_manifest["versions"][version]
    version_dir = data_root / version_info["directory"]
    with (version_dir / "manifest.json").open("r", encoding="utf-8") as handle:
        version_manifest = json.load(handle)
    expected_length = int(root_manifest["token_length"])
    if int(version_manifest["token_length"]) != expected_length:
        raise ValueError(f"version {version}: inconsistent token_length")
    float_decimals = int(config["output"].get("float_decimals", 12))
    samples: list[NormalizedSpectrum] = []
    for sample_meta in root_manifest["samples"]:
        path = data_root / sample_meta["files"][version]["path"]
        grid, raw_values, missing = read_version_file(
            path, version, expected_length
        )
        normalized = normalize_spectrum(
            sample_meta=sample_meta,
            version=version,
            grid_unit=version_info["grid_unit"],
            grid=grid,
            raw_values=raw_values,
            missing_indices=missing,
            float_decimals=float_decimals,
        )
        samples.append(normalized)
    return NormalizedDataset(version=version, samples=samples, token_length=expected_length)


def load_all_versions(
    data_root: str | Path, config: dict[str, Any]
) -> dict[str, NormalizedDataset]:
    datasets = {
        version: load_normalized_dataset(data_root, version, config)
        for version in ("A", "B", "C")
    }
    reference_ids = datasets["A"].sample_ids
    for version, dataset in datasets.items():
        if dataset.sample_ids != reference_ids:
            raise ValueError(f"version {version}: sample ordering differs from version A")
        if dataset.token_length != datasets["A"].token_length:
            raise ValueError(f"version {version}: token length differs from version A")
    return datasets


def save_normalized_datasets(
    datasets: dict[str, NormalizedDataset], output_root: str | Path
) -> None:
    output_root = ensure_dir(output_root)
    for version, dataset in datasets.items():
        values, masks = dataset.stacked()
        grids = np.stack([sample.grid for sample in dataset.samples])
        np.savez_compressed(
            output_root / f"version_{version}.npz",
            sample_ids=np.asarray(dataset.sample_ids),
            material_families=np.asarray(
                [sample.material_family for sample in dataset.samples]
            ),
            instrument_sources=np.asarray(
                [sample.instrument_source for sample in dataset.samples]
            ),
            is_rare=np.asarray(
                [sample.is_rare for sample in dataset.samples], dtype=np.int8
            ),
            token_values=values,
            masks=masks,
            grids=grids,
        )
        write_json(
            output_root / f"version_{version}_manifest.json",
            {
                "version": version,
                "token_length": dataset.token_length,
                "sample_count": len(dataset.samples),
                "grid_units": sorted(
                    {sample.grid_unit for sample in dataset.samples}
                ),
                "token_values_sha256": array_sha256(values),
                "masks_sha256": array_sha256(masks),
            },
        )
