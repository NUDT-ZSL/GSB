"""Deterministic local generation of target images and material ground truth.

The target is simply the forward renderer evaluated at the material truth
stored in the description.  Because every geometry and shading operation is
deterministic, the target is reproducible bit-for-bit across runs without any
downloaded resource.
"""

from __future__ import annotations

import os
from typing import Any, Dict

import numpy as np

from .shading import render_image


def truth_params(config: Dict[str, Any]) -> np.ndarray:
    material = config["material"]["truth"]
    return np.array(
        [float(material["albedo"]), float(material["roughness"])], dtype=np.float64
    )


def initial_params(config: Dict[str, Any]) -> np.ndarray:
    material = config["material"]["initial"]
    return np.array(
        [float(material["albedo"]), float(material["roughness"])], dtype=np.float64
    )


def generate_target(config: Dict[str, Any], geometry: Dict[str, Any]) -> np.ndarray:
    """Render the deterministic target image at the ground-truth parameters."""
    return render_image(truth_params(config), geometry)


def save_target(path: str, target: np.ndarray) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, "wb") as handle:
        np.save(handle, target.astype(np.float64), allow_pickle=False)
    os.replace(tmp_path, path)


def load_or_generate_target(
    config: Dict[str, Any], geometry: Dict[str, Any], target_file: str = None
) -> np.ndarray:
    """Load a cached target if present, otherwise generate and optionally save."""
    if target_file and os.path.exists(target_file):
        target = np.load(target_file, allow_pickle=False)
        expected = (geometry["pixel_count"], 3)
        if target.shape != expected:
            raise ValueError(
                "cached target shape %s does not match expected %s"
                % (target.shape, expected)
            )
        return target.astype(np.float64)
    target = generate_target(config, geometry)
    if target_file:
        save_target(target_file, target)
    return target
