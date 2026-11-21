"""Deterministic seam / gap candidate localization."""

from __future__ import annotations

import numpy as np

from .features import compute_features, frame_bounds
from .util import checksum_obj, hash_obj


def _refine_gap_bounds(start, end, frame_len):
    """Inner frame-exact core of a frame-level gap.

    Frame quantization can extend a detected gap past the true silence by
    up to one frame on each side, so the declared gap interval is the core
    [start + frame_len, end - frame_len): every sample in the core is
    covered only by silent frames.  Gaps too short for a core keep their
    frame-level bounds.
    """
    if end - start > 2 * frame_len:
        return start + frame_len, end - frame_len
    return start, end


def detect_seam_candidates(pcm, config, hop=None):
    """Locate recording gaps and splice seams.

    Returns a dict with candidates ({sample_index, kind, score}), gap
    intervals, and the framing actually applied.  Deterministic: identical
    (pcm, config) -> identical output; score ties break by ascending index.
    """
    hop = hop or config.frame_hop
    frame_len = config.frame_len
    energy, zcr = compute_features(pcm, frame_len, hop)
    n = energy.size

    gaps = []
    gap_candidates = []
    i = 0
    while i < n:
        if energy[i] < config.gap_energy_threshold:
            j = i
            while j + 1 < n and energy[j + 1] < config.gap_energy_threshold:
                j += 1
            start, _ = frame_bounds(i, frame_len, hop)
            _, end = frame_bounds(j, frame_len, hop)
            end = min(end, pcm.size)
            start, end = _refine_gap_bounds(start, end, frame_len)
            gaps.append([int(start), int(end)])
            gap_candidates.append(
                {
                    "sample_index": int(start),
                    "kind": "gap",
                    "score": round(float(config.gap_energy_threshold - energy[i]), 12),
                }
            )
            i = j + 1
        else:
            i += 1

    # Declared framing rule: a splice seam is a discontinuity with no silent
    # frame within +/- splice_silence_window_frames frames.  Onsets from or
    # offsets into silence always have a silent frame nearby and are
    # recording boundaries, not splices.
    silent = energy < config.gap_energy_threshold
    window = config.splice_silence_window_frames
    splice_candidates = []
    if n >= 2:
        d_energy = np.abs(np.diff(energy))
        d_zcr = np.abs(np.diff(zcr))
        for k in range(n - 1):
            kb = k + 1
            lo = max(0, kb - window)
            hi = min(n, kb + window + 1)
            if not silent[lo:hi].any() and (
                d_energy[k] >= config.seam_energy_threshold
                or d_zcr[k] >= config.seam_zcr_threshold
            ):
                boundary, _ = frame_bounds(k + 1, frame_len, hop)
                score = float(
                    d_energy[k] / max(config.seam_energy_threshold, 1e-12)
                    + d_zcr[k] / max(config.seam_zcr_threshold, 1e-12)
                )
                splice_candidates.append(
                    {
                        "sample_index": int(boundary),
                        "kind": "splice",
                        "score": round(score, 12),
                    }
                )

    # non-maximum suppression: keep the strongest candidate per frame_len window
    splice_candidates.sort(key=lambda c: (-c["score"], c["sample_index"]))
    kept = []
    for cand in splice_candidates:
        if all(abs(cand["sample_index"] - k["sample_index"]) >= frame_len for k in kept):
            kept.append(cand)

    all_candidates = kept + gap_candidates
    # deterministic cap: score desc, sample index asc breaks ties
    all_candidates.sort(key=lambda c: (-c["score"], c["sample_index"]))
    cap = min(config.max_seam_candidates, config.resource.max_seam_candidates)
    capped = all_candidates[:cap]
    capped.sort(key=lambda c: c["sample_index"])

    return {
        "candidates": capped,
        "gaps": sorted(gaps),
        "hop": hop,
        "frame_len": frame_len,
        "num_frames": int(n),
        "truncated": len(all_candidates) > cap,
    }


def seam_set_version(seam_result):
    """Content version + checksum for a detected seam set."""
    body = {
        "candidates": seam_result["candidates"],
        "gaps": seam_result["gaps"],
        "hop": seam_result["hop"],
        "frame_len": seam_result["frame_len"],
    }
    return {"hash": hash_obj(body), "checksum": checksum_obj(body)}
