"""按确定性规则生成多设备 WAV 样本与会话 JSON。

生成模型与引擎估计模型互为独立实现：
    n_obs = n_theory + offset_ms/1000*rate + drift_ppm*1e-6*n_theory
内容为低幅值确定性正弦混合；脉冲为幅度 0.9 的三采样尖峰，
峰值精确落在理论时刻的设备本地采样点上。
可选 missing_pulses 让部分脉冲不写入（标记仍保留在会话中），
用于验证可观测性降级；也支持让脉冲理论时刻靠近段边界模拟段外场景。
"""

import argparse
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mocap_audio.wavio import write_wav


def _tone(frame_count, channels, seed):
    rng = np.random.RandomState(seed)
    freqs = rng.uniform(120.0, 880.0, size=channels)
    phases = rng.uniform(0.0, 2 * np.pi, size=channels)
    n = np.arange(frame_count, dtype=np.float64)
    data = np.zeros((frame_count, channels), dtype=np.float64)
    for ch in range(channels):
        data[:, ch] = (0.05 * np.sin(2 * np.pi * freqs[ch] * n / 48000.0
                                     + phases[ch])
                       + 0.02 * np.sin(2 * np.pi * (freqs[ch] * 1.5) * n
                                       / 48000.0))
    return data


def _stamp_pulse(data, sample_index, channels):
    pattern = np.array([0.45, 0.9, 0.45])
    for offset, amp in enumerate(pattern):
        idx = sample_index - 1 + offset
        if 0 <= idx < data.shape[0]:
            data[idx, :] = amp


def generate(spec_path, out_root):
    with open(spec_path, "r", encoding="utf-8") as fh:
        spec = json.load(fh)
    os.makedirs(out_root, exist_ok=True)
    wav_dir = os.path.join(out_root, "wav")
    os.makedirs(wav_dir, exist_ok=True)

    session = {
        "session_id": spec["session_id"],
        "snapshot_id": spec.get("snapshot_id", "snapshot-%s" % spec["session_id"]),
        "model_order": spec["model_order"],
        "stat_window_ms": spec["stat_window_ms"],
        "absolute_gate_db": spec["absolute_gate_db"],
        "relative_gate_db": spec["relative_gate_db"],
        "dr_lower_db": spec["dr_lower_db"],
        "dr_upper_db": spec["dr_upper_db"],
        "target_sample_rate": spec["target_sample_rate"],
        "target_bits": spec["target_bits"],
        "timeslice_ms": spec["timeslice_ms"],
        "concurrency": spec["concurrency"],
        "memory_budget_bytes": spec["memory_budget_bytes"],
        "state_dir": spec["state_dir"],
        "output_wav": spec["output_wav"],
        "output_report": spec["output_report"],
        "output_channels": spec["output_channels"],
        "tail_rule": spec.get("tail_rule", "include"),
        "devices": [],
    }
    generation_meta = []

    for dev_index, dev in enumerate(spec["devices"]):
        dev_entry = {
            "device_id": dev["device_id"],
            "sample_rate": dev["sample_rate"],
            "bits": dev["bits"],
            "is_reference": dev.get("is_reference", False),
            "segments": [],
        }
        offset_ms = dev.get("offset_ms", 0.0)
        drift_ppm = dev.get("drift_ppm", 0.0)
        for seg_index, seg in enumerate(dev["segments"]):
            rate = dev["sample_rate"]
            frame_count = int(round(seg["duration_ms"] * rate / 1000.0))
            channels = seg["channels"]
            data = _tone(frame_count, len(channels),
                         seed=1000 + dev_index * 100 + seg_index)
            offset_samples = offset_ms * rate / 1000.0
            drift = drift_ppm * 1e-6
            missing = set(seg.get("missing_pulses", []))
            pulse_entries = []
            stamped = []
            for p_index, pulse_ms in enumerate(seg["pulse_times_ms"]):
                # 会话中的脉冲时刻为相对段起点的毫秒数
                theory = pulse_ms * rate / 1000.0
                observed = theory + offset_samples + drift * theory
                peak = int(round(observed))
                if p_index not in missing and 0 <= peak < frame_count:
                    _stamp_pulse(data, peak, len(channels))
                    stamped.append(pulse_ms)
                pulse_entries.append({
                    "theoretical_ms": pulse_ms,
                    "search_ms": seg.get("search_ms", 5),
                })
            file_name = "%s__%s.wav" % (dev["device_id"], seg["segment_id"])
            file_path = os.path.join(wav_dir, file_name)
            write_wav(file_path, data, rate, dev["bits"])
            dev_entry["segments"].append({
                "segment_id": seg["segment_id"],
                "file": os.path.relpath(file_path, out_root).replace("\\", "/"),
                "start_ms": seg["start_ms"],
                "duration_ms": seg["duration_ms"],
                "channels": channels,
                "gain": seg["gain"],
                "pulses": pulse_entries,
            })
            generation_meta.append({
                "device_id": dev["device_id"],
                "segment_id": seg["segment_id"],
                "offset_ms": offset_ms,
                "drift_ppm": drift_ppm,
                "stamped_pulses_ms": stamped,
                "missing_pulses": sorted(missing),
            })
        session["devices"].append(dev_entry)

    session_path = os.path.join(out_root, "session.json")
    with open(session_path, "w", encoding="utf-8") as fh:
        json.dump(session, fh, ensure_ascii=False, indent=2)
    with open(os.path.join(out_root, "generation_meta.json"), "w",
              encoding="utf-8") as fh:
        json.dump(generation_meta, fh, ensure_ascii=False, indent=2)
    print(session_path)


def main():
    parser = argparse.ArgumentParser(description="生成确定性多设备 WAV 样本")
    parser.add_argument("--spec", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    generate(args.spec, args.out)


if __name__ == "__main__":
    main()

