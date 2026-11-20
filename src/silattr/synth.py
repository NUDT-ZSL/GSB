"""本地确定性通话样本合成：16kHz 单声道 PCM + 帧级观测 + 清单。

固定种子下输出逐字节可复现：噪声底含缓慢漂移与突变台阶，
语音活动段与静音段按确定性时间轴拼接，静音中含可控串入人声残留。
"""

import os

import numpy as np

from .config import CATEGORY_CROSSTALK
from .models import CallManifest, FrameObs

SAMPLE_RATE = 16000


def _noise_floor_curve(n_frames, rng):
    """生成噪声底真值曲线（dBFS）：缓慢漂移 + 若干突变台阶。"""
    base = -52.0
    drift_freq = rng.uniform(0.004, 0.012)
    drift_amp = rng.uniform(2.0, 4.0)
    phase = rng.uniform(0.0, 2 * np.pi)
    curve = base + drift_amp * np.sin(
        2 * np.pi * drift_freq * np.arange(n_frames) + phase
    )
    step_count = int(rng.integers(2, 5))
    for _ in range(step_count):
        pos = int(rng.integers(n_frames // 8, n_frames - n_frames // 8))
        height = float(rng.choice([-1.0, 1.0]) * rng.uniform(7.0, 12.0))
        curve[pos:] += height
    return curve


def _speech_timeline(n_frames, rng):
    """确定性语音/静音时间轴：返回 (is_speech, speaker_id, crosstalk) 三数组。"""
    is_speech = np.zeros(n_frames, dtype=bool)
    speaker_id = np.full(n_frames, -1, dtype=np.int64)
    crosstalk = np.zeros(n_frames, dtype=bool)
    pos = int(rng.integers(4, 10))
    n_speakers = int(rng.integers(2, 4))
    while pos < n_frames - 8:
        talk_len = int(rng.integers(18, 45))
        gap_len = int(rng.integers(10, 30))
        speaker = int(rng.integers(0, n_speakers))
        end = min(pos + talk_len, n_frames)
        is_speech[pos:end] = True
        speaker_id[pos:end] = speaker
        # 在约 35% 的静音段中放入 3~7 帧串入人声残留
        gap_end = min(end + gap_len, n_frames)
        if rng.random() < 0.35 and gap_end - end >= 6:
            c_start = end + int(rng.integers(1, 3))
            c_len = int(rng.integers(3, 7))
            crosstalk[c_start:min(c_start + c_len, gap_end)] = True
        pos = gap_end
    return is_speech, speaker_id, crosstalk


def synthesize_call(call_id, seed, duration_ms, frame_ms=20):
 """合成单条通话，返回 pcm(int16)、帧观测列表与噪声底/时间轴数组。"""
    frame_len = SAMPLE_RATE * frame_ms // 1000
    n_frames = duration_ms // frame_ms
    n_samples = n_frames * frame_len
    rng = np.random.default_rng(seed)

    floor_db = _noise_floor_curve(n_frames, rng)
    is_speech, speaker_id, crosstalk = _speech_timeline(n_frames, rng)

    time_samples = np.arange(n_samples, dtype=np.float64)
    pcm = np.zeros(n_samples, dtype=np.float64)
    for frame in range(n_frames):
        s0, s1 = frame * frame_len, (frame + 1) * frame_len
        t = time_samples[s0:s1] / SAMPLE_RATE
        floor_gain = 10.0 ** (floor_db[frame] / 20.0)
        noise = rng.standard_normal(frame_len) * floor_gain
        pcm[s0:s1] += noise
        if is_speech[frame]:
            f0 = float(rng.uniform(110.0, 170.0))
            envelope = 0.5 + 0.5 * np.sin(
                2 * np.pi * float(rng.uniform(2.0, 4.0)) * t
                + float(rng.uniform(0.0, 2 * np.pi))
            )
            tone = (
                np.sin(2 * np.pi * f0 * t)
                + 0.4 * np.sin(2 * np.pi * 2 * f0 * t)
            )
            pcm[s0:s1] += 0.18 * envelope * tone
        elif crosstalk[frame]:
            f0 = float(rng.uniform(120.0, 200.0))
            pcm[s0:s1] += (
                0.03 * np.sin(2 * np.pi * f0 * t + float(rng.uniform(0, 2 * np.pi)))
            )

    peak = float(np.max(np.abs(pcm)))
    if peak > 0:
        pcm = pcm * (32767.0 / max(peak, 1.0)) * 0.9
    pcm_i16 = np.clip(np.rint(pcm), -32768, 32767).astype(np.int16)

    observations = []
    for frame in range(n_frames):
        observations.append(
            FrameObs(
                frame_index=frame,
                sample_start=frame * frame_len,
                sample_end=(frame + 1) * frame_len,
                noise_floor_true=float(floor_db[frame]),
                is_speech=bool(is_speech[frame]),
                speaker_id=int(speaker_id[frame]) if is_speech[frame] else None,
            )
        )
    metadata = {
        "is_speech": is_speech,
        "speaker_id": speaker_id,
        "crosstalk": crosstalk,
        "floor_db": floor_db,
        "frame_len": frame_len,
        "crosstalk_category": CATEGORY_CROSSTALK,
    }
    manifest = CallManifest(
        call_id=call_id,
        seed=seed,
        duration_ms=duration_ms,
        frame_ms=frame_ms,
        noise_profile_ref=f"profile://{call_id}/seed/{seed}",
    )
    return pcm_i16, observations, manifest, metadata


def write_call_dataset(out_dir, calls):
    """写出 PCM、帧级观测 JSONL 与通话清单 JSONL。calls=[(call_id, seed, duration_ms)]。"""
    os.makedirs(out_dir, exist_ok=True)
    manifests = []
    for call_id, seed, duration_ms in calls:
        pcm, observations, manifest, _ = synthesize_call(call_id, seed, duration_ms)
        pcm_path = os.path.join(out_dir, f"{call_id}.pcm")
        pcm.tofile(pcm_path)
        obs_path = os.path.join(out_dir, f"{call_id}.frames.jsonl")
        with open(obs_path, "w", encoding="utf-8") as handle:
            import json

            for obs in observations:
                handle.write(json.dumps(obs.to_dict(), ensure_ascii=False) + "\n")
        manifests.append(manifest)
    manifest_path = os.path.join(out_dir, "calls.jsonl")
    with open(manifest_path, "w", encoding="utf-8") as handle:
        import json

        for manifest in manifests:
            handle.write(json.dumps(manifest.to_dict(), ensure_ascii=False) + "\n")
    return manifest_path

