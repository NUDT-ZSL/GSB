"""Scale observation and deterministic fine/approximate path arbitration."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from numbers import Real
from typing import Any

from .catalog import ACTIVE_EVENT_TYPES
from .codes import BusinessCode
from .config import DetectionConfig
from .errors import AedError
from .io import canonical_json_bytes
from .paths import PathProfile, path_profiles

SELECTION_POLICY = (
    "1. hard resident-memory cap",
    "2. candidate-comparison budget",
    "3. fine-path frame-scale cap",
    "4. measured/estimated recall benefit",
)


@dataclass(frozen=True)
class ScaleObservation:
    dialog_count: int
    total_frames: int
    avg_candidates_per_frame: float
    speaker_count: int
    feature_dimension: int
    dialog_ids: tuple[str, ...] = ()
    recall_gain_estimate: float = 0.05

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "dialog_count": self.dialog_count,
            "total_frames": self.total_frames,
            "avg_candidates_per_frame": round(float(self.avg_candidates_per_frame), 6),
            "speaker_count": self.speaker_count,
            "feature_dimension": self.feature_dimension,
            "recall_gain_estimate": round(float(self.recall_gain_estimate), 6),
        }
        if self.dialog_ids:
            payload["dialog_ids"] = list(self.dialog_ids)
        return payload


@dataclass(frozen=True)
class ResourceConstraints:
    max_resident_mb: float = 8.0
    max_candidate_comparisons: int = 2_000_000
    max_fine_frames: int = 50_000
    min_recall_gain: float = 0.02

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_resident_mb": float(self.max_resident_mb),
            "max_candidate_comparisons": int(self.max_candidate_comparisons),
            "max_fine_frames": int(self.max_fine_frames),
            "min_recall_gain": float(self.min_recall_gain),
        }


def _invalid(field: str, message: str) -> AedError:
    return AedError(BusinessCode.E_CONFIG_INVALID, message, field=field)


def _validate_observation(observation: ScaleObservation) -> None:
    integer_fields = ("dialog_count", "total_frames", "speaker_count", "feature_dimension")
    for field in integer_fields:
        value = getattr(observation, field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise _invalid(field, f"{field} must be a positive integer")
    if not isinstance(observation.avg_candidates_per_frame, Real) or isinstance(
        observation.avg_candidates_per_frame, bool
    ):
        raise _invalid(
            "avg_candidates_per_frame",
            "avg_candidates_per_frame must be numeric",
        )
    if not math.isfinite(float(observation.avg_candidates_per_frame)) or float(
        observation.avg_candidates_per_frame
    ) < 0:
        raise _invalid(
            "avg_candidates_per_frame",
            "avg_candidates_per_frame must be a non-negative finite number",
        )
    if not math.isfinite(float(observation.recall_gain_estimate)):
        raise _invalid("recall_gain_estimate", "recall_gain_estimate must be finite")
    if observation.dialog_ids and len(observation.dialog_ids) != observation.dialog_count:
        raise _invalid(
            "dialog_ids",
            "dialog_ids length must match dialog_count",
        )


def _validate_constraints(constraints: ResourceConstraints) -> None:
    if not math.isfinite(float(constraints.max_resident_mb)) or constraints.max_resident_mb <= 0:
        raise _invalid("max_resident_mb", "max_resident_mb must be positive")
    if (
        isinstance(constraints.max_candidate_comparisons, bool)
        or not isinstance(constraints.max_candidate_comparisons, int)
        or constraints.max_candidate_comparisons < 0
    ):
        raise _invalid(
            "max_candidate_comparisons",
            "max_candidate_comparisons must be a non-negative integer",
        )
    if (
        isinstance(constraints.max_fine_frames, bool)
        or not isinstance(constraints.max_fine_frames, int)
        or constraints.max_fine_frames < 0
    ):
        raise _invalid("max_fine_frames", "max_fine_frames must be a non-negative integer")
    if not math.isfinite(float(constraints.min_recall_gain)):
        raise _invalid("min_recall_gain", "min_recall_gain must be finite")


def _resident_mb(
    total_frames: int,
    processed_frames: int,
    observed_feature_dimension: int,
    candidate_count: int,
    dialog_count: int,
    speaker_count: int,
) -> float:
    # Feature matrix + 5-class score matrix + labels + candidate/decision overhead.
    # The canonical full feature matrix is retained even on the approximate path.
    feature_bytes = total_frames * observed_feature_dimension * 8
    score_bytes = processed_frames * (len(ACTIVE_EVENT_TYPES) + 1) * 8
    label_bytes = total_frames * 16
    candidate_bytes = candidate_count * 128
    fixed_overhead = 64 * 1024 + 256 * 1024
    per_speaker_bookkeeping = dialog_count * speaker_count * 4096
    return round(
        (
            feature_bytes
            + score_bytes
            + label_bytes
            + candidate_bytes
            + fixed_overhead
            + per_speaker_bookkeeping
        )
        / (1024.0 * 1024.0),
        6,
    )


def _estimate(
    observation: ScaleObservation,
    profile: PathProfile,
) -> dict[str, Any]:
    stride = profile.analysis_stride_frames
    processed_frames = int(math.ceil(observation.total_frames / stride))
    candidate_count = int(round(observation.total_frames * observation.avg_candidates_per_frame))
    comparison_count = (
        int(math.ceil(candidate_count / stride))
        * len(ACTIVE_EVENT_TYPES)
        * profile.classifier_passes
    )
    resident_mb = _resident_mb(
        observation.total_frames,
        processed_frames,
        observation.feature_dimension,
        candidate_count,
        observation.dialog_count,
        observation.speaker_count,
    )
    return {
        "path": profile.name,
        "feature_dimension": profile.feature_dimension,
        "analysis_stride_frames": stride,
        "analysis_hop_ms": profile.to_dict()["analysis_hop_ms"],
        "classifier_passes": profile.classifier_passes,
        "processed_frames": processed_frames,
        "coverage_frames": observation.total_frames,
        "candidate_frame_count_estimate": candidate_count,
        "candidate_comparisons": comparison_count,
        "resident_mb_estimate": resident_mb,
        "estimated_recall_gain_vs_approximate": round(
            float(observation.recall_gain_estimate), 6
        ),
    }


def decide_path(
    observation: ScaleObservation,
    constraints: ResourceConstraints | None = None,
    *,
    detection_config: DetectionConfig | None = None,
) -> dict[str, Any]:
    """Select a path using hard resources first, then expected recall benefit."""

    constraints = constraints or ResourceConstraints()
    detection_config = detection_config or DetectionConfig()
    _validate_observation(observation)
    _validate_constraints(constraints)
    profiles = path_profiles(detection_config)
    fine = profiles["fine"]
    approximate = profiles["approximate"]
    fine_estimate = _estimate(observation, fine)
    approximate_estimate = _estimate(observation, approximate)

    reasons: list[str] = []
    fine_allowed = True
    if fine_estimate["resident_mb_estimate"] > constraints.max_resident_mb:
        fine_allowed = False
        reasons.append(
            "fine resident-memory estimate "
            f"{fine_estimate['resident_mb_estimate']:.3f} MB exceeds "
            f"{constraints.max_resident_mb:.3f} MB"
        )
    if fine_estimate["candidate_comparisons"] > constraints.max_candidate_comparisons:
        fine_allowed = False
        reasons.append(
            "fine candidate comparisons "
            f"{fine_estimate['candidate_comparisons']} exceed "
            f"{constraints.max_candidate_comparisons}"
        )
    if observation.total_frames > constraints.max_fine_frames:
        fine_allowed = False
        reasons.append(
            f"total frames {observation.total_frames} exceed fine cap "
            f"{constraints.max_fine_frames}"
        )
    if observation.recall_gain_estimate < constraints.min_recall_gain:
        fine_allowed = False
        reasons.append(
            "estimated fine recall gain "
            f"{observation.recall_gain_estimate:.4f} is below required "
            f"{constraints.min_recall_gain:.4f}"
        )

    selected = "fine" if fine_allowed else "approximate"
    approximate_allowed = (
        approximate_estimate["resident_mb_estimate"] <= constraints.max_resident_mb
        and approximate_estimate["candidate_comparisons"]
        <= constraints.max_candidate_comparisons
    )
    if selected == "fine":
        reasons = [
            "all hard budgets pass and estimated recall gain meets the decision threshold"
        ]
    elif not reasons:
        reasons = ["approximate path selected for efficiency"]
    if selected == "approximate" and not approximate_allowed:
        reasons.append(
            "approximate path is the best-effort fallback but also exceeds a hard budget"
        )

    dialog_ids = observation.dialog_ids or tuple(
        f"dialog_{index:03d}" for index in range(observation.dialog_count)
    )
    path_by_dialog = {dialog_id: selected for dialog_id in dialog_ids}
    payload: dict[str, Any] = {
        "selected_path": selected,
        "degraded": selected == "approximate",
        "degradation_reason": (
            "; ".join(reasons) if selected == "approximate" else None
        ),
        "constraints_satisfied": approximate_allowed if selected == "approximate" else True,
        "selection_policy": list(SELECTION_POLICY),
        "decision_reasons": reasons,
        "observation": observation.to_dict(),
        "constraints": constraints.to_dict(),
        "estimates": {
            "fine": fine_estimate,
            "approximate": approximate_estimate,
        },
        "path_by_dialog": path_by_dialog,
        "planned_coverage": {
            "dialog_count": observation.dialog_count,
            "total_frames": observation.total_frames,
            "all_dialogs": len(path_by_dialog) == observation.dialog_count,
            "all_frames": True,
            "frame_scope": "canonical 20 ms frame / 10 ms hop lattice",
        },
    }
    fingerprint = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
    payload["decision_sha256"] = fingerprint
    return payload
