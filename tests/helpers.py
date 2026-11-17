from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[1]


def write_config(
    directory: Path,
    *,
    name: str = "config.yaml",
    rows: int = 240,
    data_spec_name: str = "data_spec.yaml",
    **overrides: Any,
) -> Path:
    with (ROOT / "configs" / "data_spec.yaml").open("r", encoding="utf-8") as handle:
        data_spec = yaml.safe_load(handle)
    data_spec["rows"] = int(rows)
    with (directory / data_spec_name).open("w", encoding="utf-8") as handle:
        yaml.safe_dump(data_spec, handle, sort_keys=False, allow_unicode=True)

    with (ROOT / "configs" / "example.yaml").open("r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle)
    config["data_spec"] = data_spec_name
    config["experiment"].update(
        {
            "n_trees": 4,
            "max_depth": 2,
            "learning_rate": 0.1,
            "row_subsample": 0.85,
            "col_subsample": 0.80,
            "min_samples_leaf": 4,
            "n_bins": 8,
            "precision": "float32",
            "tolerance": 1.0e-6,
        }
    )
    config["runtime"].update({"n_jobs": 2, "snapshot_interval": 2})
    config["diagnostics"].update(
        {
            "decision_threshold": 0.5,
            "include_baseline": False,
            "fault_tree_indices": [],
        }
    )
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(config.get(key), dict):
            config[key].update(copy.deepcopy(value))
        else:
            config[key] = copy.deepcopy(value)
    path = directory / name
    with path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(config, handle, sort_keys=False, allow_unicode=True)
    return path


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def rewrite_config(path: Path, **overrides: Any) -> Path:
    with path.open("r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(config.get(key), dict):
            config[key].update(copy.deepcopy(value))
        else:
            config[key] = copy.deepcopy(value)
    with path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(config, handle, sort_keys=False, allow_unicode=True)
    return path

