"""Endpoint merging and deterministic post-processing."""
from __future__ import annotations

from .errors import E_GAP_MERGED, E_SEG_TOO_SHORT
from .state_machine import SILENCE

_ACTIVE_EXCLUDE = (SILENCE,)


def _raw_intervals(trace: list[dict]) -> list[dict]:
    intervals: list[dict] = []
    start: int | None = None
    for entry in trace:
        active = entry["state"] not in _ACTIVE_EXCLUDE
        if active and start is None:
            start = entry["frame"]
        elif not active and start is not None:
            intervals.append({"start_frame": start, "end_frame": entry["frame"] - 1})
            start = None
    if start is not None:
        intervals.append({"start_frame": start, "end_frame": trace[-1]["frame"]})
    return intervals


def _frame_ms(frame: int, hop_ms: int) -> int:
    return frame * hop_ms


def postprocess(trace: list[dict], feature_rows: list[dict], config) -> tuple[list[dict], list[dict]]:
    hop = config.frame_hop_ms
    min_speech_frames = max(1, (config.min_speech_ms + hop - 1) // hop)
    min_silence_frames = max(1, (config.min_silence_ms + hop - 1) // hop)
    energy_by_frame = {row["frame"]: row["log_energy"] for row in feature_rows}
    audit: list[dict] = []

    intervals = _raw_intervals(trace)

    # Step 1: drop intervals shorter than the minimum speech segment.
    kept: list[dict] = []
    for interval in intervals:
        length = interval["end_frame"] - interval["start_frame"] + 1
        if length < min_speech_frames:
            audit.append({
                "code": E_SEG_TOO_SHORT,
                "start_frame": interval["start_frame"],
                "end_frame": interval["end_frame"],
                "length_frames": length,
                "detail": f"length_frames<{min_speech_frames}",
            })
        else:
            kept.append(interval)

    # Step 2: merge gaps shorter than the minimum silence interval.
    merged: list[dict] = []
    for interval in kept:
        if merged:
            gap_start = merged[-1]["end_frame"] + 1
            gap_end = interval["start_frame"] - 1
            gap_len = gap_end - gap_start + 1
            if gap_len < min_silence_frames:
                audit.append({
                    "code": E_GAP_MERGED,
                    "start_frame": gap_start,
                    "end_frame": gap_end,
                    "length_frames": gap_len,
                    "detail": f"gap_frames<{min_silence_frames}",
                })
                merged[-1]["end_frame"] = interval["end_frame"]
                continue
        merged.append(dict(interval))

    # Step 3: trim head/tail silence (only silence outside speech survives above;
    # segments already start/end on active frames, so record the trim boundaries).
    if trace:
        first_active = next((e["frame"] for e in trace if e["state"] != SILENCE), None)
        last_active = next((e["frame"] for e in reversed(trace) if e["state"] != SILENCE), None)
        if first_active is not None and first_active > 0:
            audit.append({"code": "E_HEAD_SILENCE_TRIMMED", "start_frame": 0,
                          "end_frame": first_active - 1,
                          "length_frames": first_active})
        if last_active is not None and last_active < trace[-1]["frame"]:
            audit.append({"code": "E_TAIL_SILENCE_TRIMMED",
                          "start_frame": last_active + 1,
                          "end_frame": trace[-1]["frame"],
                          "length_frames": trace[-1]["frame"] - last_active})

    segments: list[dict] = []
    for index, interval in enumerate(merged):
        energies = [energy_by_frame[f] for f in
                    range(interval["start_frame"], interval["end_frame"] + 1)
                    if f in energy_by_frame]
        avg_energy = round(sum(energies) / len(energies), config.feature_decimals) if energies else None
        segments.append({
            "segment_id": index,
            "start_frame": interval["start_frame"],
            "end_frame": interval["end_frame"],
            "start_ms": _frame_ms(interval["start_frame"], hop),
            "end_ms": _frame_ms(interval["end_frame"] + 1, hop),
            "avg_log_energy": avg_energy,
        })
    return segments, audit
