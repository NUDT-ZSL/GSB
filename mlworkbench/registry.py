"""Default deterministic label-set registry and projection presets."""
from .config import LabelSet, ProjectionParams


def default_labelsets():
    v1 = LabelSet(
        version=1,
        labels=("animal", "vehicle", "plant", "structure"),
        thresholds={"animal": 0.5, "vehicle": 0.5, "plant": 0.5, "structure": 0.5},
        prevalence={"animal": 0.45, "vehicle": 0.35, "plant": 0.40, "structure": 0.30},
        uncertain_rate=0.50,
        gt_seed=101,
        score_seed=202,
    )
    v2 = LabelSet(
        version=2,
        labels=("animal", "vehicle", "plant", "structure", "sky"),
        thresholds={"animal": 0.5, "vehicle": 0.55, "plant": 0.45,
                    "structure": 0.5, "sky": 0.5},
        prevalence={"animal": 0.45, "vehicle": 0.35, "plant": 0.40,
                    "structure": 0.30, "sky": 0.50},
        uncertain_rate=0.50,
        gt_seed=101,
        score_seed=202,
    )
    return {1: v1, 2: v2}


def default_projection():
    return ProjectionParams(version=1, out_dim=24, seed=7)
