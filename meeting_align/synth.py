"""Deterministic local sample generation.

Synthesizes two recording batches of 16 kHz mono PCM from a fixed seed:
per-speaker base frequency / formants / energy envelopes are drawn from the
seed, utterances are placed on a fixed timeline with fixed gains, and
recording gaps, splice seams and repeated fragments are inserted at known
positions.  Batch transcripts (ms start/end, speaker, per-word confidence)
and the seam insertion description are emitted alongside the PCM.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from .util import hash_pcm

SAMPLE_RATE = 16000
DEFAULT_SEED = 20241122


def _make_speakers(rng, n_speakers=3):
    speakers = []
    for _ in range(n_speakers):
        f0 = float(rng.uniform(95.0, 210.0))
        formants = sorted(float(f) for f in rng.uniform(300.0, 3200.0, size=3))
        bandwidths = [float(rng.uniform(60.0, 140.0)) for _ in range(3)]
        harm_phases = [float(rng.uniform(0, 2 * np.pi)) for _ in range(8)]
        syll_rate = float(rng.uniform(2.5, 4.0))
        speakers.append(
            {
                "f0": f0,
                "formants": formants,
                "bandwidths": bandwidths,
                "harm_phases": harm_phases,
                "syll_rate": syll_rate,
            }
        )
    return speakers


def _formant_weight(speaker, freq):
    w = 0.0
    for formant, bw in zip(speaker["formants"], speaker["bandwidths"]):
        w += float(np.exp(-0.5 * ((freq - formant) / bw) ** 2))
    return w


def render_utterance(speaker, dur_ms, gain, sr=SAMPLE_RATE):
    """Render one utterance; deterministic for a fixed speaker dict."""
    n = int(round(dur_ms * sr / 1000.0))
    t = np.arange(n, dtype=np.float64) / sr
    f0 = speaker["f0"] * (1.0 + 0.008 * np.sin(2 * np.pi * 4.5 * t))
    phase = 2 * np.pi * np.cumsum(f0) / sr
    sig = np.zeros(n, dtype=np.float64)
    for h in range(1, 9):
        w = _formant_weight(speaker, h * speaker["f0"])
        sig += w * np.sin(h * phase + speaker["harm_phases"][h - 1])
    attack = np.minimum(1.0, np.arange(n) / (0.02 * sr))
    release = np.minimum(1.0, (n - np.arange(n)) / (0.03 * sr))
    syll = 1.0 + 0.3 * np.sin(2 * np.pi * speaker["syll_rate"] * t)
    sig *= np.minimum(attack, release) * syll
    peak = np.max(np.abs(sig)) or 1.0
    return sig / peak * gain * 0.6


def _build_timeline(rng, speakers, plan, sr=SAMPLE_RATE):
    """plan: list of (speaker_idx, start_ms, dur_ms). Returns float buffer."""
    total_ms = max(start + dur for _, start, dur in plan) + 200
    buf = np.zeros(int(round(total_ms * sr / 1000.0)), dtype=np.float64)
    gains = []
    for spk, start_ms, dur_ms in plan:
        gain = float(rng.uniform(0.55, 1.0))
        gains.append(round(gain, 6))
        utt = render_utterance(speakers[spk], dur_ms, gain, sr)
        s0 = int(round(start_ms * sr / 1000.0))
        buf[s0 : s0 + len(utt)] += utt
    return buf, gains


def _apply_artifacts(buf, speakers, rng, artifacts, sr=SAMPLE_RATE):
    """Insert gaps (zeros), splices (abrupt overwrite) and repeated fragments."""
    truth = {"gaps": [], "splices": [], "repeats": []}
    for art in artifacts:
        kind = art["kind"]
        if kind == "gap":
            s0 = int(round(art["start_ms"] * sr / 1000.0))
            s1 = int(round(art["end_ms"] * sr / 1000.0))
            buf[s0:s1] = 0.0
            truth["gaps"].append([s0, s1])
        elif kind == "splice":
            s0 = int(round(art["start_ms"] * sr / 1000.0))
            s1 = int(round(art["end_ms"] * sr / 1000.0))
            spk = speakers[art["speaker"]]
            chunk = render_utterance(spk, art["end_ms"] - art["start_ms"], 1.0, sr)
            buf[s0:s1] = chunk[: s1 - s0]
            truth["splices"].append([s0, s1])
        elif kind == "repeat":
            src0 = int(round(art["src_start_ms"] * sr / 1000.0))
            src1 = int(round(art["src_end_ms"] * sr / 1000.0))
            dst0 = int(round(art["dst_start_ms"] * sr / 1000.0))
            chunk = buf[src0:src1].copy()
            buf[dst0 : dst0 + len(chunk)] = chunk
            truth["repeats"].append(
                {"src": [src0, src1], "dst": [dst0, dst0 + len(chunk)]}
            )
            truth["splices"].append([dst0, dst0 + len(chunk)])
        else:  # pragma: no cover - defensive
            raise ValueError(f"unknown artifact {kind}")
    truth["splices"].sort()
    return truth


def _make_transcript(rng, batch_id, plan, words_per_sec=3.2):
    segments = []
    for idx, (spk, start_ms, dur_ms) in enumerate(plan):
        n_words = max(2, int(round(dur_ms / 1000.0 * words_per_sec)))
        step = dur_ms / n_words
        words = []
        for w in range(n_words):
            words.append(
                {
                    "text": f"{batch_id.lower()}w{idx}_{w}",
                    "offset_ms": round(w * step, 3),
                    "confidence": round(float(rng.uniform(0.6, 0.99)), 4),
                }
            )
        segments.append(
            {
                "id": f"{batch_id}{idx}",
                "batch_id": batch_id,
                "speaker": f"spk{spk}",
                "start_ms": float(start_ms),
                "end_ms": float(start_ms + dur_ms),
                "words": words,
            }
        )
    return segments


BATCH_A_PLAN = [
    (0, 200, 2000),
    (1, 2400, 2200),
    (2, 4800, 2000),
    (0, 7000, 2200),
    (1, 9400, 2400),
]
BATCH_A_ARTIFACTS = [
    {"kind": "gap", "start_ms": 3000, "end_ms": 3400},
    {"kind": "splice", "start_ms": 5600, "end_ms": 6000, "speaker": 0},
    {"kind": "repeat", "src_start_ms": 7200, "src_end_ms": 7600, "dst_start_ms": 9800},
]

BATCH_B_PLAN = [
    (2, 100, 2000),
    (0, 2300, 2200),
    (1, 4700, 2400),
    (2, 7300, 2200),
]
BATCH_B_ARTIFACTS = [
    {"kind": "gap", "start_ms": 5200, "end_ms": 5550},
    {"kind": "splice", "start_ms": 7800, "end_ms": 8200, "speaker": 1},
]
BATCH_B_DECLARED_RATE = 15992  # misdeclared recorder clock, within tolerance

BATCH_B_V2_ARTIFACTS = [
    {"kind": "gap", "start_ms": 2600, "end_ms": 2950},
    {"kind": "splice", "start_ms": 7800, "end_ms": 8200, "speaker": 1},
]


def _to_pcm(buf):
    return (np.clip(buf, -1.0, 1.0) * 32767.0).astype("<i2")


def generate_sample_set(out_dir, seed=DEFAULT_SEED):
    """Generate the full deterministic sample set. Returns the manifest dict."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)
    speakers = _make_speakers(rng)

    buf_a, gains_a = _build_timeline(rng, speakers, BATCH_A_PLAN)
    truth_a = _apply_artifacts(buf_a, speakers, rng, BATCH_A_ARTIFACTS)
    buf_b, gains_b = _build_timeline(rng, speakers, BATCH_B_PLAN)
    truth_b = _apply_artifacts(buf_b, speakers, rng, BATCH_B_ARTIFACTS)
    buf_b2 = buf_b.copy()
    # rebuild B v2 from the clean timeline so artifact positions differ
    rng_v2 = np.random.default_rng(seed + 1)
    buf_b2, _ = _build_timeline(rng_v2, speakers, BATCH_B_PLAN)
    truth_b2 = _apply_artifacts(buf_b2, speakers, rng_v2, BATCH_B_V2_ARTIFACTS)

    pcm_a, pcm_b, pcm_b2 = _to_pcm(buf_a), _to_pcm(buf_b), _to_pcm(buf_b2)
    (out / "batchA.pcm").write_bytes(pcm_a.tobytes())
    (out / "batchB.pcm").write_bytes(pcm_b.tobytes())
    (out / "batchB_v2.pcm").write_bytes(pcm_b2.tobytes())

    transcripts = {
        "A": {
            "declared_sample_rate": SAMPLE_RATE,
            "segments": _make_transcript(rng, "A", BATCH_A_PLAN),
        },
        "B": {
            "declared_sample_rate": BATCH_B_DECLARED_RATE,
            "segments": _make_transcript(rng, "B", BATCH_B_PLAN),
        },
    }
    (out / "transcripts.json").write_text(json.dumps(transcripts, indent=1))

    seam_truth = {
        "A": truth_a,
        "B": truth_b,
        "B_v2": truth_b2,
        "note": "sample-index positions of inserted gaps / splices / repeats",
    }
    (out / "seam_truth.json").write_text(json.dumps(seam_truth, indent=1))

    manifest = {
        "seed": seed,
        "sample_rate": SAMPLE_RATE,
        "order": ["A", "B"],
        "batches": {
            "A": {
                "pcm": "batchA.pcm",
                "num_samples": int(pcm_a.size),
                "sha256": hash_pcm(pcm_a),
                "declared_sample_rate": SAMPLE_RATE,
                "utterance_gains": gains_a,
            },
            "B": {
                "pcm": "batchB.pcm",
                "num_samples": int(pcm_b.size),
                "sha256": hash_pcm(pcm_b),
                "declared_sample_rate": BATCH_B_DECLARED_RATE,
                "utterance_gains": gains_b,
            },
            "B_v2": {
                "pcm": "batchB_v2.pcm",
                "num_samples": int(pcm_b2.size),
                "sha256": hash_pcm(pcm_b2),
                "declared_sample_rate": BATCH_B_DECLARED_RATE,
                "utterance_gains": gains_b,
            },
        },
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1))
    return manifest


def load_pcm(path):
    return np.frombuffer(Path(path).read_bytes(), dtype="<i2").copy()
