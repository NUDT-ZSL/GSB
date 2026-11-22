"""Sample-index authority and millisecond delivery conversion."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from .errors import timebase_mismatch


def _round_half_up_ratio(numerator: int, denominator: int) -> int:
    if denominator <= 0:
        raise ValueError("denominator must be positive")
    return (2 * numerator + denominator) // (2 * denominator)


def sample_to_ms(sample_index: int, sample_rate: int) -> float:
    """Convert samples to milliseconds, rounded half-up to three decimals."""
    if sample_index < 0:
        return -sample_to_ms(-sample_index, sample_rate)
    micro_ms = _round_half_up_ratio(sample_index * 1_000_000, sample_rate)
    return micro_ms / 1000.0


def ms_to_sample(delivery_ms: float | str, sample_rate: int) -> int:
    """Invert delivered milliseconds with explicit half-up rounding."""
    value = Decimal(str(delivery_ms))
    samples = value * Decimal(sample_rate) / Decimal(1000)
    return int(samples.to_integral_value(rounding=ROUND_HALF_UP))


def frame_start_sample(frame_index: int, hop_samples: int) -> int:
    return frame_index * hop_samples


def frame_end_sample(frame_index: int, hop_samples: int, frame_samples: int) -> int:
    return frame_index * hop_samples + frame_samples


def span_end_sample(end_frame_exclusive: int, hop_samples: int, frame_samples: int) -> int:
    if end_frame_exclusive <= 0:
        return 0
    return (end_frame_exclusive - 1) * hop_samples + frame_samples


@dataclass
class SegmentSpan:
    segment_id: str
    speaker_id: int
    start_frame: int
    end_frame: int
    start_sample: int
    end_sample: int
    mask_score: float = 0.0
    frame_indices: tuple[int, ...] = ()

    @property
    def frame_count(self) -> int:
        return self.end_frame - self.start_frame

    def start_ms(self, sample_rate: int) -> float:
        return sample_to_ms(self.start_sample, sample_rate)

    def end_ms(self, sample_rate: int) -> float:
        return sample_to_ms(self.end_sample, sample_rate)


def build_span(
    *,
    segment_id: str,
    speaker_id: int,
    start_frame: int,
    end_frame: int,
    sample_rate: int,
    frame_samples: int,
    hop_samples: int,
    mask_score: float = 0.0,
) -> SegmentSpan:
    start_sample = frame_start_sample(start_frame, hop_samples)
    end_sample = span_end_sample(end_frame, hop_samples, frame_samples)
    return SegmentSpan(
        segment_id=segment_id,
        speaker_id=speaker_id,
        start_frame=start_frame,
        end_frame=end_frame,
        start_sample=start_sample,
        end_sample=end_sample,
        mask_score=mask_score,
        frame_indices=tuple(range(start_frame, end_frame)),
    )


def verify_span_timebase(
    span: SegmentSpan,
    *,
    sample_rate: int,
    frame_samples: int,
    hop_samples: int,
) -> dict[str, Any]:
    start_ms = span.start_ms(sample_rate)
    end_ms = span.end_ms(sample_rate)
    if span.end_frame <= span.start_frame:
        raise timebase_mismatch(
            "segment spans zero frames",
            segment_id=span.segment_id,
            start_frame=span.start_frame,
            end_frame=span.end_frame,
            start_ms=start_ms,
            end_ms=end_ms,
        )
    expected_start = frame_start_sample(span.start_frame, hop_samples)
    expected_end = span_end_sample(span.end_frame, hop_samples, frame_samples)
    if span.start_sample != expected_start or span.end_sample != expected_end:
        raise timebase_mismatch(
            "segment sample indices do not match its frame interval",
            segment_id=span.segment_id,
            start_frame=span.start_frame,
            end_frame=span.end_frame,
            start_sample=span.start_sample,
            end_sample=span.end_sample,
            expected_start_sample=expected_start,
            expected_end_sample=expected_end,
        )
    inverse_start_sample = ms_to_sample(start_ms, sample_rate)
    inverse_end_sample = ms_to_sample(end_ms, sample_rate)
    if inverse_start_sample != span.start_sample or inverse_end_sample != span.end_sample:
        raise timebase_mismatch(
            "delivered milliseconds do not invert to the authoritative samples",
            segment_id=span.segment_id,
            start_frame=span.start_frame,
            end_frame=span.end_frame,
            start_ms=start_ms,
            end_ms=end_ms,
            inverse_start_sample=inverse_start_sample,
            inverse_end_sample=inverse_end_sample,
        )
    inverse_start_frame = inverse_start_sample // hop_samples
    inverse_end_frame = (
        (inverse_end_sample - frame_samples) // hop_samples + 1
        if inverse_end_sample >= frame_samples
        else 0
    )
    if inverse_start_frame != span.start_frame or inverse_end_frame != span.end_frame:
        raise timebase_mismatch(
            "delivered milliseconds do not invert to the authoritative frames",
            segment_id=span.segment_id,
            start_frame=span.start_frame,
            end_frame=span.end_frame,
            start_ms=start_ms,
            end_ms=end_ms,
            inverse_frame_interval=[inverse_start_frame, inverse_end_frame],
        )
    return {
        "segment_id": span.segment_id,
        "speaker_id": span.speaker_id,
        "start_frame": span.start_frame,
        "end_frame": span.end_frame,
        "start_sample": span.start_sample,
        "end_sample": span.end_sample,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "inverse_start_frame": inverse_start_frame,
        "inverse_end_frame": inverse_end_frame,
        "inverse_start_sample": inverse_start_sample,
        "inverse_end_sample": inverse_end_sample,
        "rounding_rule": "half_up_0.001ms",
    }


def verify_delivery_interval(
    *,
    segment_id: str,
    start_ms: float | str,
    end_ms: float | str,
    start_frame: int,
    end_frame: int,
    sample_rate: int,
    frame_samples: int,
    hop_samples: int,
) -> dict[str, Any]:
    """Validate supplied delivered milliseconds against authoritative frames."""
    if end_frame <= start_frame:
        raise timebase_mismatch(
            "delivered segment spans zero frames",
            segment_id=segment_id,
            start_frame=start_frame,
            end_frame=end_frame,
            start_ms=start_ms,
            end_ms=end_ms,
        )
    expected_start = frame_start_sample(start_frame, hop_samples)
    expected_end = span_end_sample(end_frame, hop_samples, frame_samples)
    canonical_start = sample_to_ms(expected_start, sample_rate)
    canonical_end = sample_to_ms(expected_end, sample_rate)
    if Decimal(str(start_ms)) != Decimal(str(canonical_start)) or Decimal(
        str(end_ms)
    ) != Decimal(str(canonical_end)):
        raise timebase_mismatch(
            "delivered milliseconds are not the canonical frame-derived values",
            segment_id=segment_id,
            start_frame=start_frame,
            end_frame=end_frame,
            start_ms=start_ms,
            end_ms=end_ms,
            canonical_start_ms=canonical_start,
            canonical_end_ms=canonical_end,
        )
    span = SegmentSpan(
        segment_id=segment_id,
        speaker_id=0,
        start_frame=start_frame,
        end_frame=end_frame,
        start_sample=expected_start,
        end_sample=expected_end,
    )
    return verify_span_timebase(
        span,
        sample_rate=sample_rate,
        frame_samples=frame_samples,
        hop_samples=hop_samples,
    )


def assert_no_overlap(spans: list[SegmentSpan], *, sample_rate: int) -> None:
    ordered = sorted(spans, key=lambda item: (item.start_sample, item.end_sample, item.speaker_id))
    for previous, current in zip(ordered, ordered[1:]):
        if current.start_sample < previous.end_sample:
            raise timebase_mismatch(
                "separated segments overlap",
                segment_id=current.segment_id,
                start_frame=current.start_frame,
                end_frame=current.end_frame,
                previous_segment_id=previous.segment_id,
                previous_frame_interval=[previous.start_frame, previous.end_frame],
                sample_rate=sample_rate,
            )
