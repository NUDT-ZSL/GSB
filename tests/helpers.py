from __future__ import annotations

import copy
import json
from pathlib import Path

from dimdiag.config import load_config
from dimdiag.data import write_demo_sample


def prepare_workspace(root: Path):
    write_demo_sample(root)
    return root / "configs" / "demo.json"


def base_config(config_path: Path):
    return copy.deepcopy(load_config(config_path))


def generous_tolerance(config, value: float = 1.0):
    for key in config["error_tolerance"]:
        config["error_tolerance"][key] = value
    return config


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )

