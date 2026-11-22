"""Deterministic local sample generation.

A sample is fully determined by (seed, image_size): fixed-size RGB array.
Ground truth is fully determined by (seed, LabelSet): multi-label set plus
an uncertain subset. No external state is consulted.
"""
import hashlib
from dataclasses import dataclass

import numpy as np


def generate_pixels(seed, size):
    rng = np.random.default_rng(seed)
    return rng.integers(0, 256, size=(size, size, 3), dtype=np.uint8)


def _unit_interval(*parts):
    h = hashlib.sha256("|".join(str(p) for p in parts).encode()).digest()
    return int.from_bytes(h[:4], "big") / 2**32


def deterministic_annotation(seed, label_set):
    """Ground-truth (labels, uncertain) for a sample seed under a label set."""
    labels, uncertain = set(), set()
    for label in label_set.labels:
        p = _unit_interval("gt", label_set.gt_seed, seed, label, label_set.version)
        if p < label_set.prevalence[label]:
            labels.add(label)
            u = _unit_interval("unc", label_set.gt_seed, seed, label, label_set.version)
            if u < label_set.uncertain_rate:
                uncertain.add(label)
    return labels, uncertain


@dataclass
class Sample:
    image_id: str
    seed: int
    pixels: np.ndarray


def make_dataset(seeds, image_size):
    return [
        Sample(image_id=f"img-{seed:04d}", seed=seed, pixels=generate_pixels(seed, image_size))
        for seed in seeds
    ]
