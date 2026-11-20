"""Lineage records and replay of historical frame decisions / segments.

A lineage record pins the exact cache keys and key components (feature
version, noise estimate version, threshold version, node path) under which a
decision was produced. Replay re-derives each stage from the recorded
upstream artifacts and compares field by field; any missing record or
mismatch raises E_LINEAGE_BROKEN naming the broken node and the first
mismatched field.
"""

from __future__ import annotations

import numpy as np

from . import errors
from .codec import loads
from .nodes import compute_decision, compute_segments, replay_noise_scalar


def build_lineage(clip_id, keys, components, param_version):
    return {
        "clip_id": clip_id,
        "node_path": ["features", "noise", "decision", "segments"],
        "param_version": param_version,
        "keys": dict(keys),
        "components": components,
    }


def _broken(node, field, **extra):
    return errors.VadError(
        errors.E_LINEAGE_BROKEN,
        f"lineage broken at node '{node}', first mismatched field '{field}'",
        node=node,
        field=field,
        **extra,
    )


def _load_artifact(cache, lineage, node):
    key = lineage.get("keys", {}).get(node)
    if key is None:
        raise _broken(node, "key")
    raw = cache.get(key)
    if raw is None:
        raise _broken(node, "artifact", key=key)
    return loads(raw)


def _replay_through_decision(cache, lineage):
    components = lineage.get("components")
    if not isinstance(components, dict):
        raise _broken("features", "components")
    feat = _load_artifact(cache, lineage, "features")
    noise_art = _load_artifact(cache, lineage, "noise")
    energy = feat["matrix"][:, 0]
    noise_comp = components.get("noise", {})
    for field in ("percentile", "percentile_window"):
        if field in noise_comp and noise_art.get(field) != noise_comp[field]:
            raise _broken("noise", field)
    replayed = replay_noise_scalar(energy, noise_art["used_frames"])
    if replayed != noise_art["noise"]:
        raise _broken("noise", "noise")
    dec_art = _load_artifact(cache, lineage, "decision")
    dec_comp = components.get("decision", {})
    recomputed = compute_decision(
        energy,
        noise_art["noise"],
        dec_comp.get("mult_low"),
        dec_comp.get("mult_high"),
    )
    for field in ("low_threshold", "high_threshold"):
        if recomputed[field] != dec_art.get(field):
            raise _broken("decision", field)
    cached_dec = np.asarray(dec_art["decisions"], dtype=np.bool_)
    if recomputed["decisions"].shape != cached_dec.shape:
        raise _broken("decision", "decisions.shape")
    diff = np.nonzero(recomputed["decisions"] != cached_dec)[0]
    if diff.size:
        raise _broken("decision", f"decisions[{int(diff[0])}]")
    return feat, noise_art, dec_art


def replay_frame_decision(cache, lineage, frame_index):
    """Replay and return the historical decision of one frame."""
    _, _, dec_art = _replay_through_decision(cache, lineage)
    decisions = dec_art["decisions"]
    if not 0 <= frame_index < len(decisions):
        raise _broken("decision", "frame_index", frame_index=frame_index)
    return bool(decisions[frame_index])


def replay_segment(cache, lineage, segment_index):
    """Replay and return one historical segment conclusion."""
    _, _, dec_art = _replay_through_decision(cache, lineage)
    seg_art = _load_artifact(cache, lineage, "segments")
    components = lineage["components"]
    feat_comp = components.get("features", {})
    seg_comp = components.get("segments", {})
    recomputed = compute_segments(
        dec_art["decisions"],
        feat_comp.get("frame_ms"),
        feat_comp.get("hop_ms"),
        seg_comp.get("duration_ms"),
        seg_comp.get("min_speech_ms"),
        seg_comp.get("min_silence_ms"),
    )
    cached = [list(map(int, s)) for s in seg_art["segments"]]
    replayed = [list(map(int, s)) for s in recomputed["segments"]]
    if len(cached) != len(replayed):
        raise _broken("segments", "segments.length")
    for i, (a, b) in enumerate(zip(cached, replayed)):
        if a != b:
            raise _broken("segments", f"segments[{i}]")
    if not 0 <= segment_index < len(cached):
        raise _broken("segments", "segment_index", segment_index=segment_index)
    return cached[segment_index]
