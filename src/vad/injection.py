"""Fault-injection bench: perturbations modify real input samples."""
from __future__ import annotations

from pathlib import Path

import numpy as np

from .errors import E_INJECTION_FRAME, VadError
from .util import hash_json, load_json

POP = "pop"
CLIP = "clip"
MUTE = "mute"
NONFINITE = "nonfinite"
_TYPES = (POP, CLIP, MUTE, NONFINITE)


class InjectionManifest:
    def __init__(self, raw: dict):
        self.raw = raw
        self.manifest_hash = hash_json(raw)
        self.items = list(raw.get("items", []))
        for item in self.items:
            if item["type"] not in _TYPES:
                raise ValueError(f"unknown injection type: {item['type']}")

    @classmethod
    def load(cls, path: Path) -> "InjectionManifest":
        return cls(load_json(Path(path)))

    def for_sample(self, sample_name: str) -> list[dict]:
        return [item for item in self.items if item["sample"] == sample_name]


def _frame_bounds(item: dict, hop_len: int, frame_len: int, total: int) -> tuple[int, int]:
    if "frame" in item:
        start_frame = end_frame = int(item["frame"])
    else:
        start_frame, end_frame = int(item["frame_start"]), int(item["frame_end"])
    start = start_frame * hop_len
    end = min(total, end_frame * hop_len + frame_len)
    if start >= total or start_frame < 0 or end_frame < start_frame:
        raise VadError(E_INJECTION_FRAME,
                       f"injection {item['id']} frame range outside signal")
    return start, end


def apply_injections(samples: np.ndarray, items: list[dict], frame_length_ms: int,
                     frame_hop_ms: int, sample_rate: int = 16000) -> list[dict]:
    """Apply perturbations to a copy of the PCM array; return hit records."""
    frame_len = int(round(frame_length_ms * sample_rate / 1000))
    hop_len = int(round(frame_hop_ms * sample_rate / 1000))
    data = np.array(samples, dtype=np.float64, copy=True)
    total = data.shape[0]
    hits: list[dict] = []
    for item in sorted(items, key=lambda it: it["id"]):
        start, end = _frame_bounds(item, hop_len, frame_len, total)
        kind = item["type"]
        if kind == POP:
            amp = float(item.get("amplitude", 0.9))
            data[start:end] = amp * np.sign(np.sin(np.arange(end - start)))
        elif kind == CLIP:
            amp = float(item.get("amplitude", 0.98))
            data[start:end] = np.clip(data[start:end], -amp, amp)
            # Hard clipping drives extrema to the rail, altering the waveform.
            data[start:end] = np.where(np.abs(data[start:end]) >= amp,
                                       amp * np.sign(data[start:end]),
                                       data[start:end])
        elif kind == MUTE:
            data[start:end] = 0.0
        elif kind == NONFINITE:
            data[start:end] = np.nan
        hits.append({
            "injection_id": item["id"],
            "type": kind,
            "start_sample": start,
            "end_sample": end,
            "frames": sorted(set(range(start // hop_len,
                                       (end - 1) // hop_len + 1))),
        })
    return data, hits
