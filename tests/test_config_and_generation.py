from __future__ import annotations

import copy
import os
import subprocess
import sys

from prefworkbench.canonical import canonical_json, file_hash
from prefworkbench.config import default_config, normalize_config, validate_config
from prefworkbench.errors import (
    E_CONFIG_FIXED_SCALE,
    E_CONFIG_PRECISION,
    E_CONFIG_ROUNDING,
    E_CONFIG_SMOOTHING,
    E_CONFIG_THRESHOLD,
    E_CONFIG_WEIGHT,
)
from prefworkbench.generation import generate_samples, write_dataset


def _codes(config: dict) -> set[str]:
    return {error.code for error in validate_config(config)}


def test_default_config_is_valid(config: dict) -> None:
    assert validate_config(config) == []


def test_config_validation_business_codes(config: dict) -> None:
    broken = copy.deepcopy(config)
    broken["quality"]["weights"]["length"] = 0
    assert E_CONFIG_WEIGHT in _codes(broken)

    broken = copy.deepcopy(config)
    broken["quality"]["quality_threshold"] = 1.4
    assert E_CONFIG_THRESHOLD in _codes(broken)

    broken = copy.deepcopy(config)
    broken["near_duplicate"]["similarity_threshold"] = -0.1
    assert E_CONFIG_THRESHOLD in _codes(broken)

    broken = copy.deepcopy(config)
    broken["consistency"]["smoothing_alpha"] = 0
    assert E_CONFIG_SMOOTHING in _codes(broken)

    broken = copy.deepcopy(config)
    broken["precision"]["active"] = "float16"
    assert E_CONFIG_PRECISION in _codes(broken)

    broken = copy.deepcopy(config)
    broken["precision"]["fixed_scale"] = -1
    assert E_CONFIG_FIXED_SCALE in _codes(broken)

    broken = copy.deepcopy(config)
    broken["precision"]["rounding"] = "nearest"
    assert E_CONFIG_ROUNDING in _codes(broken)


def test_normalize_fills_defaults() -> None:
    normalized = normalize_config({"precision": {"active": "float32"}})
    assert normalized["precision"]["active"] == "float32"
    assert normalized["precision"]["fixed_scale"] == 1000000
    assert normalized["quality"]["weights"]["length"] > 0


def test_generation_is_byte_deterministic(tmp_path) -> None:
    first = write_dataset(tmp_path / "first", seed=77, count=32)
    second = write_dataset(tmp_path / "second", seed=77, count=32)
    assert first["samples_sha256"] == second["samples_sha256"]
    assert first["config_sha256"] == second["config_sha256"]
    assert file_hash(tmp_path / "first" / "preference_pairs.jsonl") == file_hash(
        tmp_path / "second" / "preference_pairs.jsonl"
    )
    rows_a = generate_samples(77, 32)
    rows_b = generate_samples(77, 32)
    assert canonical_json(rows_a) == canonical_json(rows_b)
    assert set(rows_a[0]) == {
        "pair_id",
        "prompt",
        "chosen",
        "rejected",
        "annotator_group",
        "quality_notes",
    }


def test_generation_is_deterministic_across_hash_seeds() -> None:
    code = (
        "from prefworkbench.generation import generate_samples;"
        "from prefworkbench.canonical import canonical_json;"
        "print(canonical_json(generate_samples(77, 24)))"
    )
    outputs = []
    for seed in ("1", "999"):
        env = dict(os.environ, PYTHONHASHSEED=seed)
        outputs.append(
            subprocess.check_output([sys.executable, "-c", code], env=env, text=True)
        )
    assert outputs[0] == outputs[1]

