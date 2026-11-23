"""Structured business errors used by every application layer."""

from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
from typing import Any


@dataclass
class AppError(Exception):
    code: str
    message: str
    stage: str
    field: str | None = None
    details: dict[str, Any] = dataclass_field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__init__(self.message)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "stage": self.stage,
        }
        if self.field is not None:
            payload["field"] = self.field
        if self.details:
            payload["details"] = self.details
        return payload


ERROR_CATALOG = {
    "E_PHONEME_OUT_OF_RANGE": "Alignment label is outside the declared controlled phoneme set.",
    "E_REF_MISSING": "A required reference clip or ref_id is missing.",
    "E_PROFILE_INVALID": "Precision profile or one of its parameters is invalid.",
    "E_ERRPROP_INCONSISTENT": "Error propagation intervals or recomputed deviations are inconsistent.",
    "E_PATH_DRIFT": "Exact and approximate paths are not comparable under the current policy.",
    "E_SNAPSHOT_ROLLBACK": "A stale or backward snapshot write was rejected.",
    "E_SAMPLE_RATE": "Audio sample rate is not the required 16000 Hz.",
    "E_SEED_SHAPE": "The seed and reference clip shape/version do not match.",
    "E_DUPLICATE_UTTERANCE": "An utterance_id appears more than once in the manifest.",
    "E_INCONSISTENT": "Cross-level consistency self-check found a mismatch.",
    "E_INPUT_INVALID": "Input or request payload is invalid.",
}


DEGRADATION_POLICY: dict[str, dict[str, str]] = {
    "E_SAMPLE_RATE": {
        "category": "input_rejected",
        "strategy": "reject_sample_continue_with_active_snapshot",
    },
    "E_SEED_SHAPE": {
        "category": "registration_skipped",
        "strategy": "keep_previous_reference_and_embedding",
    },
    "E_REF_MISSING": {
        "category": "embedding_reused",
        "strategy": "reuse_cached_embedding_for_speaker",
    },
    "E_DUPLICATE_UTTERANCE": {
        "category": "duplicate_suppressed",
        "strategy": "keep_first_utterance_and_skip_incoming",
    },
    "E_PHONEME_OUT_OF_RANGE": {
        "category": "alignment_restricted",
        "strategy": "reject_out_of_range_frame_and_keep_other_frames",
    },
    "E_PROFILE_INVALID": {
        "category": "precision_fallback",
        "strategy": "fallback_to_float64_profile",
    },
    "E_ERRPROP_INCONSISTENT": {
        "category": "diagnostics_degraded",
        "strategy": "retain_last_verified_error_spectrum",
    },
    "E_PATH_DRIFT": {
        "category": "path_arbitration_fallback",
        "strategy": "use_exact_path_for_authoritative_labels",
    },
    "E_SNAPSHOT_ROLLBACK": {
        "category": "snapshot_guard",
        "strategy": "keep_current_monotonic_snapshot",
    },
    "E_INCONSISTENT": {
        "category": "diagnostics_blocked",
        "strategy": "keep_current_snapshot_and_require_rebuild",
    },
    "E_INPUT_INVALID": {
        "category": "request_rejected",
        "strategy": "continue_with_active_state",
    },
}


def degradation_for(code: str) -> dict[str, str]:
    return dict(
        DEGRADATION_POLICY.get(
            code,
            {"category": "unknown", "strategy": "continue_with_active_state"},
        )
    )
