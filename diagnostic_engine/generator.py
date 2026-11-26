from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .config import EngineConfig
from .snapshots import V1_ANCHOR_MS, parse_v1, upgrade_v1_to_v2, write_v1
from .utils import canonical_json_bytes, pretty_json, sha256_file, write_json_atomic


DEFAULT_SEED = 20261127
DEFAULT_SAMPLE_COUNT = 384
NUMERIC_COLUMNS = ("age", "income", "tenure", "score")
CATEGORY_COLUMNS = ("region", "channel")
CATEGORY_VOCAB = {
    "region": ("east", "north", "south", "west"),
    "channel": ("app", "store", "web"),
}


@dataclass(frozen=True)
class GenerationResult:
    seed: int
    sample_count: int
    samples_path: str
    v1_path: str
    v2_path: str
    manifest_path: str
    v1_checksum: str
    v2_checksum: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "seed": self.seed,
            "sample_count": self.sample_count,
            "samples_path": self.samples_path,
            "v1_path": self.v1_path,
            "v2_path": self.v2_path,
            "manifest_path": self.manifest_path,
            "v1_checksum": self.v1_checksum,
            "v2_checksum": self.v2_checksum,
        }


def generated_parameters() -> dict[str, Any]:
    return {
        "regularization": "elastic_net",
        "strength_path": [0.6, 0.3, 0.12, 0.05, 0.02],
        "max_iter": 260,
        "tolerance": 1e-4,
        "precision": "float64",
        "fixed_scale": 8192,
        "rounding_mode": "nearest_even",
        "block_size": 64,
        "block_budget_bytes": 262_144,
        "standardization": "zscore",
        "numeric_columns": list(NUMERIC_COLUMNS),
        "category_columns": list(CATEGORY_COLUMNS),
        "category_vocab": {
            key: list(values) for key, values in CATEGORY_VOCAB.items()
        },
        "learning_rate": 0.55,
        "l1_ratio": 0.2,
        "coefficient_tolerance": 2.5e-2,
        "objective_tolerance": 3.0e-2,
        "injections": [],
    }


def _category_effects(region: str, channel: str) -> float:
    region_effect = {
        "east": 0.42,
        "north": 0.12,
        "south": -0.32,
        "west": 0.05,
    }[region]
    channel_effect = {"app": 0.25, "store": -0.18, "web": 0.08}[channel]
    return region_effect + channel_effect


def generate_samples(
    output_path: str | Path,
    *,
    seed: int = DEFAULT_SEED,
    sample_count: int = DEFAULT_SAMPLE_COUNT,
) -> Path:
    if sample_count <= 0:
        raise ValueError("sample_count must be positive")
    rng = np.random.default_rng(seed)
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for index in range(sample_count):
        age = float(np.clip(rng.normal(38.0, 11.0), 18.0, 75.0))
        income = float(np.clip(rng.lognormal(3.75, 0.42), 5.0, 220.0))
        tenure = float(rng.uniform(0.0, 12.0))
        score = float(np.clip(rng.normal(0.56, 0.17), 0.0, 1.0))
        region = str(rng.choice(np.array(CATEGORY_VOCAB["region"])))
        channel = str(rng.choice(np.array(CATEGORY_VOCAB["channel"])))
        logit = (
            0.78 * (age - 38.0) / 11.0
            + 0.55 * (income - 42.5) / 23.0
            - 0.38 * (tenure - 6.0) / 3.5
            - 0.52 * (score - 0.56) / 0.17
            + _category_effects(region, channel)
            + float(rng.normal(0.0, 0.35))
        )
        row = {
            "sample_id": f"S{index + 1:06d}",
            "age": round(age, 6),
            "income": round(income, 6),
            "tenure": round(tenure, 6),
            "score": round(score, 6),
            "region": region,
            "channel": channel,
            "label": int(logit > 0.0),
        }
        lines.append(
            canonical_json_bytes(row).decode("utf-8")
        )
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


def generate_dataset(
    output_directory: str | Path,
    *,
    seed: int = DEFAULT_SEED,
    sample_count: int = DEFAULT_SAMPLE_COUNT,
) -> GenerationResult:
    root = Path(output_directory)
    samples_path = root / "samples.jsonl"
    generate_samples(samples_path, seed=seed, sample_count=sample_count)
    parameters = generated_parameters()
    base_config = EngineConfig.from_parameters(
        parameters,
        snapshot_id="snap-20261127-v1",
        version=1,
        generation=1,
        created_at_ms=V1_ANCHOR_MS + 1_234_567,
        checksum="",
        metadata_sources={
            "created_at_ms": "generator.relative_seconds=1234.567",
            "generation": "generator.generation=1",
        },
    )
    v1_path = root / "snapshots" / "v1" / "config.json"
    write_v1(base_config, v1_path)
    v2_path = root / "snapshots" / "v2"
    migration = upgrade_v1_to_v2(v1_path, v2_path)
    # Re-read both generations to ensure migration did not alter parameters.
    reloaded_v1 = parse_v1(v1_path)
    reloaded_v2 = migration.config
    if reloaded_v1.parameters() != reloaded_v2.parameters():
        raise RuntimeError("v1/v2 migration changed unified parameters")
    manifest = {
        "generator": "diagnostic_engine.generator",
        "version": 1,
        "seed": seed,
        "sample_count": sample_count,
        "numeric_columns": list(NUMERIC_COLUMNS),
        "category_columns": list(CATEGORY_COLUMNS),
        "category_vocab": {
            key: list(values) for key, values in CATEGORY_VOCAB.items()
        },
        "samples_sha256": sha256_file(samples_path),
        "v1": {
            "path": str(v1_path.relative_to(root)),
            "checksum": reloaded_v1.checksum,
            "version": reloaded_v1.version,
            "generation": reloaded_v1.generation,
            "created_at_ms": reloaded_v1.created_at_ms,
        },
        "v2": {
            "path": str(v2_path.relative_to(root)),
            "checksum": reloaded_v2.checksum,
            "version": reloaded_v2.version,
            "generation": reloaded_v2.generation,
            "created_at_ms": reloaded_v2.created_at_ms,
        },
    }
    manifest_path = root / "manifest.json"
    write_json_atomic(manifest_path, manifest)
    (root / "README.txt").write_text(
        pretty_json(
            {
                "seed": seed,
                "samples": str(samples_path.name),
                "v1_snapshot": "snapshots/v1/config.json",
                "v2_snapshot": "snapshots/v2",
                "regenerate": (
                    ".venv/bin/python -m diagnostic_engine generate --out data"
                ),
            }
        ),
        encoding="utf-8",
    )
    return GenerationResult(
        seed=seed,
        sample_count=sample_count,
        samples_path=str(samples_path),
        v1_path=str(v1_path),
        v2_path=str(v2_path),
        manifest_path=str(manifest_path),
        v1_checksum=reloaded_v1.checksum,
        v2_checksum=reloaded_v2.checksum,
    )

