"""Deterministic synthetic spectra and the three delivered format versions."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .utils import array_sha256, ensure_dir, write_json


MATERIAL_PEAK_CENTERS: dict[str, tuple[float, ...]] = {
    "oxide": (0.18, 0.34, 0.55, 0.78),
    "alloy": (0.12, 0.29, 0.47, 0.73),
    "polymer": (0.10, 0.24, 0.50, 0.66),
    "rare_earth": (0.14, 0.39, 0.61, 0.86),
    "halide": (0.08, 0.32, 0.58, 0.91),
}

DEFAULT_CENTERS = (0.15, 0.34, 0.56, 0.79)


@dataclass(frozen=True)
class GeneratedSample:
    sample_id: str
    material_family: str
    instrument_source: str
    modality: str
    is_rare: bool
    missing_indices: tuple[int, ...]
    profile: np.ndarray
    mask: np.ndarray
    wavenumber_cm1: np.ndarray
    wavelength_nm: np.ndarray
    angle_deg: np.ndarray


def _allocate_material_counts(
    families: list[str], weights: dict[str, float], sample_count: int
) -> dict[str, int]:
    normalized = np.asarray([max(0.0, float(weights[item])) for item in families])
    total = float(normalized.sum())
    if total <= 0.0:
        normalized = np.ones(len(families), dtype=np.float64)
        total = float(normalized.sum())
    normalized = normalized / total
    raw = normalized * sample_count
    counts = np.floor(raw).astype(int)
    remainder = sample_count - int(counts.sum())
    if remainder:
        order = sorted(
            range(len(families)),
            key=lambda index: (-(raw[index] - counts[index]), index),
        )
        for index in order[:remainder]:
            counts[index] += 1
    return {family: int(count) for family, count in zip(families, counts)}


def _make_profile(
    rng: np.random.Generator,
    family: str,
    instrument_index: int,
    token_length: int,
    noise_std: float,
) -> np.ndarray:
    positions = np.arange(token_length, dtype=np.float64)
    centers = MATERIAL_PEAK_CENTERS.get(family, DEFAULT_CENTERS)
    profile = np.full(token_length, float(rng.uniform(0.015, 0.06)))
    profile += np.linspace(0.0, float(rng.uniform(0.01, 0.045)), token_length)

    for center_fraction in centers:
        center = center_fraction * (token_length - 1)
        center += float(rng.normal(0.0, 0.35))
        width = float(rng.uniform(0.75, 1.85)) * (token_length / 32.0)
        amplitude = float(rng.uniform(0.35, 1.0))
        profile += amplitude * np.exp(-0.5 * ((positions - center) / width) ** 2)

    instrument_fraction = (0.24, 0.52, 0.81)[instrument_index % 3]
    instrument_center = instrument_fraction * (token_length - 1)
    instrument_width = max(0.6, 0.9 * (token_length / 32.0))
    profile += 0.24 * np.exp(
        -0.5 * ((positions - instrument_center) / instrument_width) ** 2
    )

    if noise_std > 0.0:
        profile += rng.normal(0.0, noise_std, size=token_length)
    profile = np.clip(profile, 0.0, None)
    maximum = float(profile.max())
    if maximum <= 0.0:
        profile[:] = 1.0
        maximum = 1.0
    return profile / maximum


def _make_missing_indices(
    rng: np.random.Generator,
    token_length: int,
    missing_probability: float,
    min_size: int,
    max_size: int,
    max_blocks: int,
) -> tuple[int, ...]:
    if (
        max_blocks <= 0
        or missing_probability <= 0.0
        or rng.random() >= missing_probability
    ):
        return ()
    block_count = int(rng.integers(1, max(1, max_blocks) + 1))
    occupied = np.zeros(token_length, dtype=bool)
    missing: list[int] = []
    max_allowed_missing = max(1, token_length // 3)

    for _ in range(block_count):
        block_size = int(rng.integers(max(1, min_size), max(1, max_size) + 1))
        if len(missing) + block_size > max_allowed_missing:
            block_size = max(1, max_allowed_missing - len(missing))
        if block_size <= 0:
            break
        for _attempt in range(24):
            start = int(rng.integers(0, max(1, token_length - block_size + 1)))
            span = np.arange(start, start + block_size)
            if not occupied[span].any():
                occupied[span] = True
                missing.extend(int(index) for index in span)
                break
        if len(missing) >= max_allowed_missing:
            break
    return tuple(sorted(set(missing)))


def _grid_axes(
    token_length: int, grid_config: dict[str, float]
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    wavenumber = np.linspace(
        float(grid_config["wavenumber_start_cm-1"]),
        float(grid_config["wavenumber_stop_cm-1"]),
        token_length,
        dtype=np.float64,
    )
    wavelength = 1.0e7 / wavenumber
    angle = np.linspace(
        float(grid_config["angle_start_deg"]),
        float(grid_config["angle_stop_deg"]),
        token_length,
        dtype=np.float64,
    )
    return wavenumber, wavelength, angle


def generate_samples(seed: int, config: dict[str, Any]) -> list[GeneratedSample]:
    generator_config = config["generator"]
    token_length = int(generator_config["token_length"])
    sample_count = int(generator_config["sample_count"])
    families = list(generator_config["material_families"])
    rare_families = set(generator_config["rare_material_families"])
    instruments = list(generator_config["instrument_sources"])
    material_counts = _allocate_material_counts(
        families, generator_config["material_weights"], sample_count
    )
    material_pool: list[str] = []
    for family in families:
        material_pool.extend([family] * material_counts[family])

    rng = np.random.default_rng(seed)
    rng.shuffle(material_pool)
    instrument_pool = [
        instruments[int(index) % len(instruments)] for index in rng.permutation(sample_count)
    ]
    wavenumber, wavelength, angle = _grid_axes(
        token_length, generator_config["grid"]
    )

    samples: list[GeneratedSample] = []
    for index, (family, instrument) in enumerate(zip(material_pool, instrument_pool)):
        sample_rng = np.random.default_rng(
            int(rng.integers(0, np.iinfo(np.int64).max))
        )
        instrument_index = instruments.index(instrument)
        profile = _make_profile(
            sample_rng,
            family,
            instrument_index,
            token_length,
            float(generator_config["noise_std"]),
        )
        missing = _make_missing_indices(
            sample_rng,
            token_length,
            float(generator_config["missing_probability"]),
            int(generator_config["missing_block_size_min"]),
            int(generator_config["missing_block_size_max"]),
            int(generator_config["max_missing_blocks"]),
        )
        mask = np.ones(token_length, dtype=np.int8)
        if missing:
            mask[np.asarray(missing, dtype=np.int64)] = 0
            profile[np.asarray(missing, dtype=np.int64)] = 0.0
        modality = "raman" if "Raman" in instrument else "xrd"
        samples.append(
            GeneratedSample(
                sample_id=f"S{index:04d}",
                material_family=family,
                instrument_source=instrument,
                modality=modality,
                is_rare=family in rare_families,
                missing_indices=missing,
                profile=profile,
                mask=mask,
                wavenumber_cm1=wavenumber.copy(),
                wavelength_nm=wavelength.copy(),
                angle_deg=angle.copy(),
            )
        )
    return samples


def _format_value(value: float) -> str:
    return f"{value:.17g}"


def _write_version_csv(
    path: Path,
    version: str,
    sample: GeneratedSample,
    intensity_scale: float,
    grid_unit: str,
    grid: np.ndarray,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(["token_index", "grid_unit", "grid_value", "value"])
        missing = set(sample.missing_indices)
        for index in range(len(grid)):
            if index in missing:
                if version == "A":
                    value = "NaN"
                elif version == "B":
                    value = "-1"
                else:
                    value = ""
            else:
                value = _format_value(float(sample.profile[index]) * intensity_scale)
            writer.writerow([index, grid_unit, _format_value(float(grid[index])), value])


def write_generated_dataset(
    samples: list[GeneratedSample], seed: int, config: dict[str, Any], output_root: str | Path
) -> Path:
    output_root = ensure_dir(output_root)
    generator_config = config["generator"]
    token_length = int(generator_config["token_length"])
    intensity_scales = generator_config["version_intensity_scale"]
    version_specs = {
        "A": {
            "directory": "version_A",
            "grid_unit": "wavenumber_cm-1",
            "normalization": "max_intensity",
            "missing_marker": "NaN",
        },
        "B": {
            "directory": "version_B",
            "grid_unit": "wavelength_nm",
            "normalization": "peak_area",
            "missing_marker": "negative_value",
        },
        "C": {
            "directory": "version_C",
            "grid_unit": "two_theta_deg",
            "normalization": "quantile_q90",
            "missing_marker": "empty_string",
        },
    }

    manifest_samples: list[dict[str, Any]] = []
    version_dirs: dict[str, Path] = {}
    for version, spec in version_specs.items():
        version_dir = ensure_dir(output_root / spec["directory"])
        version_dirs[version] = version_dir
        write_json(
            version_dir / "manifest.json",
            {
                "version": version,
                **spec,
                "token_length": token_length,
                "generator_seed": seed,
            },
        )

    for sample in samples:
        sample_entry = {
            "sample_id": sample.sample_id,
            "material_family": sample.material_family,
            "instrument_source": sample.instrument_source,
            "modality": sample.modality,
            "is_rare": sample.is_rare,
            "missing_token_indices": list(sample.missing_indices),
            "mask_sha256": array_sha256(sample.mask),
            "files": {},
        }
        grids = {
            "A": sample.wavenumber_cm1,
            "B": sample.wavelength_nm,
            "C": sample.angle_deg,
        }
        for version, spec in version_specs.items():
            filename = f"{sample.sample_id}.csv"
            path = version_dirs[version] / filename
            _write_version_csv(
                path,
                version,
                sample,
                float(intensity_scales[version]),
                spec["grid_unit"],
                grids[version],
            )
            sample_entry["files"][version] = {
                "path": f"{spec['directory']}/{filename}",
                "grid_unit": spec["grid_unit"],
                "intensity_scale": float(intensity_scales[version]),
            }
        manifest_samples.append(sample_entry)

    truth_profiles = np.stack([sample.profile for sample in samples])
    truth_masks = np.stack([sample.mask for sample in samples])
    np.savez_compressed(
        output_root / "generator_truth.npz",
        sample_ids=np.asarray([sample.sample_id for sample in samples]),
        material_families=np.asarray([sample.material_family for sample in samples]),
        instrument_sources=np.asarray(
            [sample.instrument_source for sample in samples]
        ),
        profiles=truth_profiles,
        masks=truth_masks,
    )
    write_json(
        output_root / "manifest.json",
        {
            "generator": "deterministic_gaussian_mixture_v1",
            "generator_seed": seed,
            "token_length": token_length,
            "sample_count": len(samples),
            "index_alignment": (
                "The generator emits row token_index in acquisition-segment order. "
                "All three versions preserve this index, so no cross-unit resampling "
                "is needed by the parser."
            ),
            "versions": version_specs,
            "material_counts": {
                family: sum(
                    sample.material_family == family for sample in samples
                )
                for family in generator_config["material_families"]
            },
            "instrument_counts": {
                instrument: sum(
                    sample.instrument_source == instrument for sample in samples
                )
                for instrument in generator_config["instrument_sources"]
            },
            "samples": manifest_samples,
        },
    )
    return output_root


def generate_and_write(
    seed: int, config: dict[str, Any], output_root: str | Path
) -> tuple[list[GeneratedSample], Path]:
    samples = generate_samples(seed, config)
    path = write_generated_dataset(samples, seed, config, output_root)
    return samples, path


def load_generator_manifest(data_root: str | Path) -> dict[str, Any]:
    with (Path(data_root) / "manifest.json").open("r", encoding="utf-8") as handle:
        return json.load(handle)
