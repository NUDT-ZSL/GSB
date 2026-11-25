"""Boundary and transcript-difference attribution on the shared frame grid."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .audio import AudioFeatures, IntervalAlignment, align_interval
from .models import Utterance


@dataclass(frozen=True)
class DifferenceAttribution:
    utterance_id: str | None
    left: IntervalAlignment
    right: IntervalAlignment | None
    start_delta_ms: int | None
    end_delta_ms: int | None
    frame_span_delta: int | None
    reasons: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "utterance_id": self.utterance_id,
            "left": self.left.to_dict(),
            "right": self.right.to_dict() if self.right else None,
            "start_delta_ms": self.start_delta_ms,
            "end_delta_ms": self.end_delta_ms,
            "frame_span_delta": self.frame_span_delta,
            "reasons": list(self.reasons),
        }


def attribute_difference(
    features: AudioFeatures,
    left: Utterance | tuple[int, int],
    right: Utterance | tuple[int, int] | None = None,
    *,
    utterance_id: str | None = None,
) -> DifferenceAttribution:
    """Explain an interval or cross-version boundary difference using VAD/frame edges."""

    left_interval = left.interval() if isinstance(left, Utterance) else left
    right_interval = right.interval() if isinstance(right, Utterance) else right if right is not None else None
    resolved_id = utterance_id
    if resolved_id is None and isinstance(left, Utterance):
        resolved_id = left.utterance_id
    if resolved_id is None and isinstance(right, Utterance):
        resolved_id = right.utterance_id

    left_alignment = align_interval(features, left_interval[0], left_interval[1], resolved_id)
    right_alignment = (
        align_interval(features, right_interval[0], right_interval[1], resolved_id)
        if right_interval is not None
        else None
    )

    reasons: list[str] = []
    start_delta = None
    end_delta = None
    frame_span_delta = None

    if right_alignment is not None:
        start_delta = right_interval[0] - left_interval[0]  # type: ignore[index]
        end_delta = right_interval[1] - left_interval[1]  # type: ignore[index]
        left_span = (
            left_alignment.last_frame - left_alignment.first_frame + 1
            if left_alignment.first_frame is not None and left_alignment.last_frame is not None
            else 0
        )
        right_span = (
            right_alignment.last_frame - right_alignment.first_frame + 1
            if right_alignment.first_frame is not None and right_alignment.last_frame is not None
            else 0
        )
        frame_span_delta = right_span - left_span
        if start_delta or end_delta:
            reasons.append("boundary_shift")
        if frame_span_delta:
            reasons.append("frame_span_change")
        if start_delta % features.config.hop_ms or end_delta % features.config.hop_ms:
            reasons.append("sub_frame_boundary_not_quantized_to_hop")
        else:
            reasons.append("boundary_lies_on_hop_grid")

    if abs(left_alignment.start_boundary_delta_ms or 0) <= features.config.frame_ms:
        reasons.append("start_close_to_vad_edge")
    if abs(left_alignment.end_boundary_delta_ms or 0) <= features.config.frame_ms:
        reasons.append("end_close_to_vad_edge")
    if left_alignment.speech_frame_ratio < 0.75:
        reasons.append("low_speech_frame_ratio_possible_silence_or_gap")
    if not reasons:
        reasons.append("stable_interval_on_shared_frame_grid")

    return DifferenceAttribution(
        utterance_id=resolved_id,
        left=left_alignment,
        right=right_alignment,
        start_delta_ms=start_delta,
        end_delta_ms=end_delta,
        frame_span_delta=frame_span_delta,
        reasons=tuple(reasons),
    )
