"""测试共享的确定性样本构造工具。"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

from mocap_audio.wavio import write_wav


def make_tone(frames, channels, seed=7):
    rng = np.random.RandomState(seed)
    freqs = rng.uniform(150.0, 700.0, size=channels)
    n = np.arange(frames, dtype=np.float64)
    data = np.empty((frames, channels), dtype=np.float64)
    for ch in range(channels):
        data[:, ch] = 0.04 * np.sin(2 * np.pi * freqs[ch] * n / 48000.0)
    return data


def stamp_pulse(data, sample_index):
    for offset, amp in enumerate((0.45, 0.9, 0.45)):
        idx = sample_index - 1 + offset
        if 0 <= idx < data.shape[0]:
            data[idx, :] = amp


def write_segment(path, rate, bits, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    write_wav(path, data, rate, bits)


def device_segment(device_id, segment_id, file_name, start_ms, duration_ms,
                   channels, gain, pulses):
    return {
        "device_id": device_id,
        "sample_rate": None,
        "bits": None,
        "is_reference": False,
        "segments": [{
            "segment_id": segment_id,
            "file": file_name,
            "start_ms": start_ms,
            "duration_ms": duration_ms,
            "channels": channels,
            "gain": gain,
            "pulses": [{"theoretical_ms": t, "search_ms": w}
                       for t, w in pulses],
        }],
    }


def base_session(tmp, devices, **overrides):
    session = {
        "session_id": "unit",
        "snapshot_id": overrides.pop("snapshot_id", "snap-unit"),
        "devices": devices,
        "model_order": overrides.pop("model_order", 1),
        "stat_window_ms": overrides.pop("stat_window_ms", 100),
        "absolute_gate_db": -70.0,
        "relative_gate_db": -10.0,
        "dr_lower_db": 6.0,
        "dr_upper_db": 40.0,
        "target_sample_rate": overrides.pop("target_sample_rate", 48000),
        "target_bits": overrides.pop("target_bits", 24),
        "timeslice_ms": overrides.pop("timeslice_ms", 200),
        "concurrency": overrides.pop("concurrency", 2),
        "memory_budget_bytes": overrides.pop("memory_budget_bytes", 1 << 24),
        "state_dir": "state",
        "output_wav": "out/program.wav",
        "output_report": "out/report.json",
        "output_channels": overrides.pop("output_channels", ["A", "B"]),
        "tail_rule": "include",
        "peak_threshold": 0.5,
    }
    session.update(overrides)
    path = os.path.join(tmp, "session.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(session, fh, ensure_ascii=False)
    return path

