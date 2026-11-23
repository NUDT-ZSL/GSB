"""Shared helpers for the workbench test-suite."""

from __future__ import annotations

import copy
import json
import os

from calib_workbench.config import DEFAULTS
from calib_workbench.data_gen import generate_manifest


def small_cfg(state_dir, out_dir, **overrides):
    cfg = copy.deepcopy(DEFAULTS)
    cfg["data"]["n_samples"] = 120
    cfg["state_dir"] = state_dir
    cfg["output_dir"] = out_dir
    cfg["scheduler"]["workers"] = 2
    cfg["scheduler"]["batch_size"] = 2
    for path, value in overrides.items():
        node = cfg
        keys = path.split(".")
        for key in keys[:-1]:
            node = node[key]
        node[keys[-1]] = value
    return cfg


def small_rows(cfg):
    return generate_manifest(cfg)


def read_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def cache_file(cfg, key):
    return os.path.join(cfg["state_dir"], "cache", key[:2], f"{key}.json")
