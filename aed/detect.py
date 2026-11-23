"""Node 2: event candidate detection and type classification."""

from __future__ import annotations

import numpy as np

from .config import PipelineConfig
from .errors import AEDError, Code
from .features import extract_features
from .synth import EVENT_TYPES, EVENT_TYPE_ID, event_waveform

_PROTO_INDEX = 0  # deterministic prototype template instance


def template_prototypes(cfg: PipelineConfig) -> np.ndarray:
    """(n_types, 4) prototype feature vectors from the template library."""
    protos = []
    for etype in EVENT_TYPES:
        wave = event_waveform(etype, cfg.template_version, _PROTO_INDEX,
                             cfg.sample_rate)
        pcm = np.clip(wave * 0.8 * 32767.0, -32768, 32767).astype("<i2")
        feats = extract_features(pcm, cfg, f"template:{etype}",
                                 len(pcm) * 1000 // cfg.sample_rate)
        protos.append(feats.mean(axis=0))
    return np.asarray(protos, dtype=np.float64)


def score_frames(feats: np.ndarray, protos: np.ndarray) -> np.ndarray:
    """Cosine similarity of every frame against every event prototype."""
    f_norm = np.linalg.norm(feats, axis=1, keepdims=True)
    p_norm = np.linalg.norm(protos, axis=1, keepdims=True)
    f = feats / np.where(f_norm > 0, f_norm, 1.0)
    p = protos / np.where(p_norm > 0, p_norm, 1.0)
    return f @ p.T


def _whiten(feats: np.ndarray, protos: np.ndarray):
    """Deterministic dialog-level standardization of feature space."""
    f = feats.copy()
    p = protos.copy()
    f[:, 0] = np.log1p(f[:, 0] * 1e6)
    p[:, 0] = np.log1p(p[:, 0] * 1e6)
    f[:, 2:] /= 8000.0
    p[:, 2:] /= 8000.0
    mu = f.mean(axis=0)
    sd = f.std(axis=0) + 1e-9
    return (f - mu) / sd, (p - mu) / sd


def detect_candidates(feats: np.ndarray, cfg: PipelineConfig) -> list:
    """Return candidate events with start/end frames, type, confidence.

    Ties on score are broken by ascending event-type id, then by
    ascending frame index.
    """
    protos = template_prototypes(cfg)
    f_z, p_z = _whiten(feats, protos)
    scores = score_frames(f_z, p_z)
    n_frames = scores.shape[0]

    best_type = np.argmax(scores, axis=1)  # lowest type id wins ties
    best_score = scores[np.arange(n_frames), best_type]
    energy_gate = f_z[:, 0] >= 0.5  # events carry above-average energy
    active = (best_score >= cfg.score_threshold) & energy_gate

    candidates = []
    fi = 0
    while fi < n_frames:
        if not active[fi]:
            fi += 1
            continue
        tid = int(best_type[fi])
        fj = fi
        while (fj + 1 < n_frames and active[fj + 1]
               and int(best_type[fj + 1]) == tid):
            fj += 1
        conf = float(np.round(best_score[fi:fj + 1].mean(), 6))
        candidates.append({
            "start_frame": fi,
            "end_frame": fj,
            "event_type": EVENT_TYPES[tid],
            "confidence": conf,
        })
        fi = fj + 1

    candidates.sort(key=lambda c: (c["start_frame"],
                                   EVENT_TYPE_ID[c["event_type"]]))
    validate_event_types(candidates)
    return candidates


def validate_event_types(candidates) -> None:
    for cand in candidates:
        if cand["event_type"] not in EVENT_TYPE_ID:
            raise AEDError(Code.E_EVENT_TYPE_INVALID,
                           "event type outside the controlled set",
                           event_type=cand["event_type"],
                           allowed=list(EVENT_TYPES))
