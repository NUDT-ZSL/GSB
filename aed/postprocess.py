"""Node 3: event segment post-processing (merge by min duration / gap)."""

from __future__ import annotations

from .config import PipelineConfig
from .errors import AEDError, Code
from .synth import EVENT_TYPE_ID


def merge_candidates(candidates, cfg: PipelineConfig,
                     duration_ms: int) -> list:
    """Pure function: rebuild millisecond segments from candidate details.

    Merges same-type candidates separated by less than min_gap_ms and
    drops merged events shorter than min_event_ms. The output is
    deterministic and can be independently rebuilt from the candidate
    list alone.
    """
    hop_ms = cfg.hop_ms
    frame_ms = cfg.frame_ms
    # disjoint frame ranges must map to disjoint ms ranges, so the
    # per-frame stride is capped at the hop when hop < frame length
    stride_ms = min(hop_ms, frame_ms)
    raw = []
    for cand in sorted(candidates,
                       key=lambda c: (c["start_frame"],
                                      EVENT_TYPE_ID[c["event_type"]])):
        start_ms = int(cand["start_frame"] * hop_ms)
        end_ms = int(cand["end_frame"] * hop_ms + stride_ms)
        raw.append({"event_type": cand["event_type"],
                    "start_ms": start_ms, "end_ms": end_ms,
                    "confidence": cand["confidence"]})

    merged = []
    for seg in raw:
        if (merged and merged[-1]["event_type"] == seg["event_type"]
                and seg["start_ms"] - merged[-1]["end_ms"] < cfg.min_gap_ms):
            prev = merged[-1]
            prev["end_ms"] = max(prev["end_ms"], seg["end_ms"])
            prev["confidence"] = round(
                (prev["confidence"] + seg["confidence"]) / 2.0, 6)
        else:
            merged.append(dict(seg))

    segments = [s for s in merged
                if s["end_ms"] - s["start_ms"] >= cfg.min_event_ms]
    for seq, seg in enumerate(segments):
        seg["event_seq"] = seq
    validate_segments(segments, duration_ms)
    return segments


def validate_segments(segments, duration_ms: int) -> None:
    prev_end = 0
    for seg in segments:
        if seg["start_ms"] < prev_end:
            raise AEDError(Code.E_OVERLAP,
                           "segments overlap or are unordered",
                           event_seq=seg.get("event_seq"),
                           start_ms=seg["start_ms"], prev_end_ms=prev_end)
        if seg["start_ms"] < 0 or seg["end_ms"] > duration_ms:
            raise AEDError(Code.E_OUT_OF_RANGE,
                           "segment outside audio duration",
                           event_seq=seg.get("event_seq"),
                           start_ms=seg["start_ms"], end_ms=seg["end_ms"],
                           duration_ms=duration_ms)
        prev_end = seg["end_ms"]
