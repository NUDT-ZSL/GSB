"""Per-sample processing pipeline and result summarisation."""
from __future__ import annotations

from collections import Counter
from pathlib import Path

from .audio import frame_signal, load_wav
from .config import VadConfig
from .features import compute_frame_features, estimate_noise_floor
from .injection import apply_injections
from .postprocess import postprocess
from .state_machine import build_thresholds, run_state_machine


def sort_key_for(path: Path) -> str:
    return path.name


def process_sample(path: Path, config: VadConfig, injections: list[dict]) -> dict:
    """Run every stage for one WAV. Never raises for domain failures."""
    name = path.name
    result: dict = {
        "sample": name,
        "sort_key": sort_key_for(path),
        "status": "ok",
        "failure": None,
        "injection_hits": [],
        "noise_floor_log_energy": None,
        "frame_count": 0,
        "frame_records": [],
        "state_trace_summary": {},
        "segments": [],
        "audit": [],
    }
    try:
        samples = load_wav(
            path,
            expected_sr=config.raw["sample_rate_hz"],
            expected_channels=config.raw["channels"],
            expected_subtype=config.raw["subtype"],
        )
        if injections:
            samples, hits = apply_injections(
                samples, injections,
                config.frame_length_ms, config.frame_hop_ms,
                config.raw["sample_rate_hz"],
            )
            result["injection_hits"] = hits
            if not np_all_finite(samples):
                # Non-finite values injected: the read/validate guard rejects the sample.
                from .errors import E_NONFINITE_SAMPLE
                raise _DomainFailure(E_NONFINITE_SAMPLE,
                                     f"{name}: non-finite sample after injection")

        frames = frame_signal(samples, config.frame_length_ms,
                              config.frame_hop_ms, config.raw["sample_rate_hz"])
        feature_rows = [compute_frame_features(frame, config.feature_decimals)
                        for frame in frames]
        noise_floor = estimate_noise_floor(feature_rows,
                                           config.noise_floor_window_frames)
        result["noise_floor_log_energy"] = round(noise_floor,
                                                 config.feature_decimals)
        thresholds = build_thresholds(noise_floor, config)
        trace = run_state_machine(feature_rows, thresholds)
        segments, audit = postprocess(trace, feature_rows, config)

        state_counts = Counter(entry["state"] for entry in trace)
        result["frame_count"] = len(feature_rows)
        result["frame_records"] = [
            {**feat, "state": tr["state"], "reason": tr["reason"]}
            for feat, tr in zip(feature_rows, trace)
        ]
        result["state_trace_summary"] = {
            "state_counts": dict(state_counts),
            "transition_count": sum(1 for tr in trace
                                    if tr["state"] != tr["previous_state"]),
            "first_frame": trace[0]["frame"] if trace else None,
            "last_frame": trace[-1]["frame"] if trace else None,
        }
        result["segments"] = segments
        result["audit"] = audit
        result["thresholds"] = {
            "low": round(thresholds.low, config.feature_decimals),
            "high": thresholds.high,
            "release": round(thresholds.release, config.feature_decimals),
            "zcr": thresholds.zcr,
            "flatness": thresholds.flatness,
        }
    except _DomainFailure as exc:
        result["status"] = "failed"
        result["failure"] = {"code": exc.code, "message": exc.message}
    except Exception as exc:  # defensive: one bad sample must never kill the run
        from .errors import E_AUDIO_FORMAT
        message = exc.message if isinstance(exc, _DomainFailure) else str(exc)
        result["status"] = "failed"
        result["failure"] = {"code": E_AUDIO_FORMAT,
                             "message": f"{name}: {message}"}
    return result


class _DomainFailure(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def np_all_finite(samples) -> bool:
    import numpy as np
    return bool(np.all(np.isfinite(samples)))
