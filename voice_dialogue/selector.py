"""Data-scale driven adaptive algorithm selection.

Scale observations (dialog count, total frames, speaker count, feature dim)
choose deterministically between:

* ``fine``      - 8D features, exact frame grid, iterative separation,
                   coordinate-descent ridge regression;
* ``approximate`` - 4D features, strided frame evaluation with anchor
                    reconstruction, single-pass separation, closed-form ridge.

The same observation always yields the same decision. Explicit priority rules
arbitrate the precision / memory / comparison conflicts and any degradation is
recorded with its category; frame boundaries are never changed by degradation.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import ComponentConfig
from .errors import BusinessError, ErrorCode

# Explicit, documented arbitration priority (highest first).
PRIORITY_RULES = (
    "P1_hard_resource_budget",
    "P2_speaker_assignment_correctness",
    "P3_frame_boundary_integrity",
    "P4_emotion_scoring_precision",
)


@dataclass
class ScaleObservation:
    num_dialogs: int
    total_frames: int
    max_speakers: int
    fine_feature_dim: int
    approx_feature_dim: int
    max_frames_per_dialog: int

    def to_dict(self) -> dict:
        return {
            "num_dialogs": self.num_dialogs,
            "total_frames": self.total_frames,
            "max_speakers": self.max_speakers,
            "fine_feature_dim": self.fine_feature_dim,
            "approx_feature_dim": self.approx_feature_dim,
            "max_frames_per_dialog": self.max_frames_per_dialog,
        }


@dataclass
class PathDecision:
    path: str
    feature_dim: int
    frame_stride: int
    separation_mode: str
    regression_mode: str
    reasons: list = field(default_factory=list)
    degradation: list = field(default_factory=list)
    projected_comparisons: int = 0
    projected_resident_frames: int = 0
    projected_regression_terms: int = 0
    priority_rules: tuple = PRIORITY_RULES

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "feature_dim": self.feature_dim,
            "frame_stride": self.frame_stride,
            "separation_mode": self.separation_mode,
            "regression_mode": self.regression_mode,
            "reasons": list(self.reasons),
            "degradation": list(self.degradation),
            "projected_comparisons": self.projected_comparisons,
            "projected_resident_frames": self.projected_resident_frames,
            "projected_regression_terms": self.projected_regression_terms,
            "priority_rules": list(self.priority_rules),
        }


def observe_scale(manifest_rows: list, config: ComponentConfig) -> ScaleObservation:
    total = sum(int(row["num_frames"]) for row in manifest_rows)
    max_speakers = max(
        (len(row["speaker_ids"]) for row in manifest_rows), default=0
    )
    max_frames = max(
        (int(row["num_frames"]) for row in manifest_rows), default=0
    )
    return ScaleObservation(
        num_dialogs=len(manifest_rows),
        total_frames=total,
        max_speakers=max_speakers,
        fine_feature_dim=config.fine_feature_dim,
        approx_feature_dim=config.approx_feature_dim,
        max_frames_per_dialog=max_frames,
    )


def _fine_eligible(obs: ScaleObservation, config: ComponentConfig) -> tuple:
    profile = config.path_profile
    checks = [
        (obs.num_dialogs <= profile.max_fine_dialogs,
         f"num_dialogs {obs.num_dialogs} exceeds fine cap "
         f"{profile.max_fine_dialogs}"),
        (obs.total_frames <= profile.max_fine_total_frames,
         f"total_frames {obs.total_frames} exceeds fine cap "
         f"{profile.max_fine_total_frames}"),
        (obs.max_speakers <= profile.max_fine_speakers,
         f"max_speakers {obs.max_speakers} exceeds fine cap "
         f"{profile.max_fine_speakers}"),
        (obs.fine_feature_dim <= profile.max_fine_feature_dim,
         f"feature_dim {obs.fine_feature_dim} exceeds fine cap "
         f"{profile.max_fine_feature_dim}"),
    ]
    failures = [reason for ok, reason in checks if not ok]
    return not failures, failures


def decide_path(
    obs: ScaleObservation, config: ComponentConfig
) -> PathDecision:
    """Apply scale thresholds, then explicit resource-priority arbitration."""
    limits = config.resource_limits
    eligible, failures = _fine_eligible(obs, config)

    # Fine-path projections (iterative separation upper bound).
    fine_comparisons = (
        obs.max_frames_per_dialog * obs.max_speakers
        * (config.fine_iterations + 1)
    )
    fine_terms = obs.max_frames_per_dialog * config.fine_feature_dim * 2
    fine_resident = obs.max_frames_per_dialog * config.fine_feature_dim

    # Approximate path defaults to an exact frame grid; stride is introduced
    # only when the resident-cell budget would otherwise be exceeded, so
    # frame boundaries are never altered and precision is sacrificed last.
    stride = 1
    approx_resident = (
        (obs.max_frames_per_dialog // stride) * config.approx_feature_dim
    )
    while (
        approx_resident > limits.max_resident_frames
        and stride < max(obs.max_frames_per_dialog, 2)
    ):
        stride *= 2
        approx_resident = (
            (obs.max_frames_per_dialog // stride) * config.approx_feature_dim
        )
    approx_comparisons = obs.max_frames_per_dialog * obs.max_speakers * 2
    approx_terms = obs.max_frames_per_dialog * 2

    resource_failures = []
    if fine_comparisons > limits.max_separation_comparisons:
        resource_failures.append(
            f"fine comparisons {fine_comparisons} > budget "
            f"{limits.max_separation_comparisons}"
        )
    if fine_resident > limits.max_resident_frames:
        resource_failures.append(
            f"fine resident frames {fine_resident} > budget "
            f"{limits.max_resident_frames}"
        )
    if fine_terms > limits.max_regression_terms:
        resource_failures.append(
            f"fine regression terms {fine_terms} > budget "
            f"{limits.max_regression_terms}"
        )

    degradation: list = []
    reasons: list = []
    if eligible and not resource_failures:
        decision = PathDecision(
            path="fine",
            feature_dim=config.fine_feature_dim,
            frame_stride=1,
            separation_mode="iterative_lloyd",
            regression_mode="coordinate_descent_ridge",
            reasons=[
                "all scale thresholds within fine profile",
                "fine resource projections within budgets",
            ],
            projected_comparisons=fine_comparisons,
            projected_resident_frames=fine_resident,
            projected_regression_terms=fine_terms,
        )
        return decision

    reasons.extend(f"scale: {f}" for f in failures)
    reasons.extend(f"resource: {f}" for f in resource_failures)
    if failures:
        degradation.append("DIMENSIONALITY_REDUCTION")
        if stride > 1:
            degradation.append("FRAME_STRIDE_APPROXIMATION")
        degradation.append("SINGLE_PASS_SEPARATION")
        degradation.append("CLOSED_FORM_REGRESSION")
    elif resource_failures:
        # P1: hard budget beats precision; degrade categories record the
        # specific precision sacrificed, frame boundaries stay untouched.
        degradation.append("RESOURCE_DRIVEN_APPROXIMATION")
        if fine_comparisons > limits.max_separation_comparisons:
            degradation.append("SINGLE_PASS_SEPARATION")
        if fine_resident > limits.max_resident_frames:
            degradation.append("DIMENSIONALITY_REDUCTION")
            if stride > 1:
                degradation.append("FRAME_STRIDE_APPROXIMATION")
        if fine_terms > limits.max_regression_terms:
            degradation.append("CLOSED_FORM_REGRESSION")

    # Even the approximate path must fit its budgets; otherwise hard fail.
    if approx_comparisons > limits.max_separation_comparisons:
        if limits.on_exceed == "error":
            raise BusinessError(
                ErrorCode.E_RESOURCE_LIMIT,
                "approximate separation still exceeds comparison budget",
                field="max_separation_comparisons",
                details={
                    "comparisons": approx_comparisons,
                    "budget": limits.max_separation_comparisons,
                },
            )
        degradation.append("COMPARISON_BUDGET_CAP")
    if approx_terms > limits.max_regression_terms and limits.on_exceed == "error":
        raise BusinessError(
            ErrorCode.E_RESOURCE_LIMIT,
            "approximate regression still exceeds term budget",
            field="max_regression_terms",
        )

    reasons.append(
        "P1 hard resource budget and scale thresholds take precedence over "
        "P4 emotion precision; P2/P3 preserved by keeping full frame index "
        "set and speaker id contract"
    )
    return PathDecision(
        path="approximate",
        feature_dim=config.approx_feature_dim,
        frame_stride=stride,
        separation_mode="single_pass_nearest_prototype",
        regression_mode="closed_form_ridge",
        reasons=reasons,
        degradation=sorted(set(degradation)),
        projected_comparisons=approx_comparisons,
        projected_resident_frames=approx_resident,
        projected_regression_terms=approx_terms,
    )
