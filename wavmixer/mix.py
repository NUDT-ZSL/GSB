"""Timeline mixing engine.

Mixing is performed in integer frame space.  Milliseconds from the manifest
are converted to frames with half-up rounding, so each clip begins at the
nearest frame boundary.  For every clip the engine applies, in order:

1. the manifest gain,
2. a linear fade-in / fade-out of up to 5 ms at each end,
3. 16-bit clipping (with a per-clip clipping flag),

then sums the resulting samples on the shared timeline and clips the sum
again (with an overall clipping flag).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .audio import SourceInfo, duration_ms_from_frames, samples_from_ms

INT16_MIN = -32768
INT16_MAX = 32767
FADE_MS = 5


def _round_half_away(value: float) -> int:
    """Round to the nearest integer, halves away from zero."""

    if value >= 0:
        return int(math.floor(value + 0.5))
    return int(math.ceil(value - 0.5))


def fade_frame_count(n_frames: int, sample_rate: int) -> int:
    """Length in frames of each end fade.

    Normally 5 ms.  Clips shorter than 10 ms use half of their length instead
    so the two ramps meet at (or before) the middle of the clip.
    """

    nominal = samples_from_ms(FADE_MS, sample_rate)
    return min(nominal, n_frames // 2)


def fade_factor(frame_index: int, n_frames: int, fade_frames: int) -> float:
    """Linear fade envelope value at *frame_index* (0.0 .. 1.0)."""

    if fade_frames <= 0:
        return 1.0
    if frame_index < fade_frames:
        return frame_index / fade_frames
    if frame_index >= n_frames - fade_frames:
        return (n_frames - 1 - frame_index) / fade_frames
    return 1.0


@dataclass(frozen=True)
class ClipMixResult:
    source: SourceInfo
    start_frame: int
    duration_frames: int
    duration_ms: int
    fade_frames: int
    clipped: bool
    peak_abs_sample: int


@dataclass(frozen=True)
class MixResult:
    sample_rate: int
    channels: int
    frame_count: int
    total_duration_ms: int
    samples: tuple[int, ...]
    clipped: bool
    clip_results: tuple[ClipMixResult, ...]


def mix_clips(clips: list[tuple[SourceInfo, int, float]]) -> MixResult:
    """Mix ``(source, offset_ms, gain)`` tuples onto one timeline.

    All sources must share sample rate, channel count and sample width; the
    pipeline validates this before calling the engine.
    """

    if not clips:
        raise ValueError("no clips to mix")

    first = clips[0][0]
    sample_rate = first.sample_rate
    channels = first.channels
    width = first.sample_width
    for source, _offset, _gain in clips:
        if (
            source.sample_rate != sample_rate
            or source.channels != channels
            or source.sample_width != width
        ):
            raise ValueError("source parameters differ; validate before mixing")

    prepared: list[tuple[SourceInfo, int, float, int]] = []
    end_frame = 0
    for source, offset_ms, gain in clips:
        start_frame = samples_from_ms(offset_ms, sample_rate)
        prepared.append(
            (
                source,
                start_frame,
                gain,
                fade_frame_count(source.n_frames, sample_rate),
            )
        )
        end_frame = max(end_frame, start_frame + source.n_frames)

    frame_count = end_frame
    mixed = [0] * (frame_count * channels)
    clip_results: list[ClipMixResult] = []
    any_mix_clipped = False

    for source, start_frame, gain, fade_frames in prepared:
        clip_clipped = False
        peak = 0
        n_frames = source.n_frames
        samples = source.samples
        for frame in range(n_frames):
            factor = fade_factor(frame, n_frames, fade_frames)
            scale = gain * factor
            out_base = (start_frame + frame) * channels
            src_base = frame * channels
            for channel in range(channels):
                value = _round_half_away(samples[src_base + channel] * scale)
                if value < INT16_MIN:
                    value = INT16_MIN
                    clip_clipped = True
                elif value > INT16_MAX:
                    value = INT16_MAX
                    clip_clipped = True
                magnitude = abs(value)
                if magnitude > peak:
                    peak = magnitude
                mixed[out_base + channel] += value
        clip_results.append(
            ClipMixResult(
                source=source,
                start_frame=start_frame,
                duration_frames=n_frames,
                duration_ms=source.duration_ms,
                fade_frames=fade_frames,
                clipped=clip_clipped,
                peak_abs_sample=peak,
            )
        )

    # Report whole milliseconds and make the WAV frame count correspond to
    # that value exactly, so ``frames == round(duration_ms * rate / 1000)``.
    # Raising the duration to the nearest millisecond at or above the true
    # end keeps ``total_duration_ms`` within 2 ms of every clip's end
    # (offset_ms + duration_ms), while never truncating audible tails.
    total_duration_ms = duration_ms_from_frames(frame_count, sample_rate)
    if total_duration_ms * sample_rate < frame_count * 1000:
        total_duration_ms += 1
    frame_count = samples_from_ms(total_duration_ms, sample_rate)
    mixed = mixed[: frame_count * channels]
    if len(mixed) < frame_count * channels:
        mixed.extend([0] * (frame_count * channels - len(mixed)))

    for index, value in enumerate(mixed):
        if value < INT16_MIN:
            mixed[index] = INT16_MIN
            any_mix_clipped = True
        elif value > INT16_MAX:
            mixed[index] = INT16_MAX
            any_mix_clipped = True

    return MixResult(
        sample_rate=sample_rate,
        channels=channels,
        frame_count=frame_count,
        total_duration_ms=total_duration_ms,
        samples=tuple(mixed),
        clipped=any_mix_clipped,
        clip_results=tuple(clip_results),
    )
