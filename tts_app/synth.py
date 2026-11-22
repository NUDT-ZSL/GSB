"""确定性合成原语：说话人参数、音素切分、声学特征、声码器、逐帧对齐。

全部随机源来自显式 seed 派生的 numpy RandomState，禁止系统时间。
"""

import hashlib

import numpy as np

SAMPLE_RATE = 16000
FRAME_MS = 20
FRAME_LEN = SAMPLE_RATE * FRAME_MS // 1000  # 320

SPEAKER_EMBED_VERSION = "spkemb_v1"

_PHONEME_MAP = {
    "a": "AA", "e": "EH", "i": "IY", "o": "OW", "u": "UW",
    "b": "B", "c": "K", "d": "D", "f": "F", "g": "G", "h": "HH",
    "j": "JH", "k": "K", "l": "L", "m": "M", "n": "N", "p": "P",
    "q": "K", "r": "R", "s": "S", "t": "T", "v": "V", "w": "W",
    "x": "KS", "y": "Y", "z": "Z",
}

_STYLE_FACTOR = {"neutral": 1.0, "excited": 1.15, "calm": 0.9, "whisper": 0.75}


def stable_seed(*parts):
    """由任意字符串部件派生 31 位确定性种子。"""
    blob = "||".join(str(p) for p in parts).encode("utf-8")
    return int.from_bytes(hashlib.sha256(blob).digest()[:4], "little") & 0x7FFFFFFF


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def speaker_params(speaker_id, seed):
    """固定种子生成说话人基频、共振峰与能量包络参数。"""
    rng = np.random.RandomState(stable_seed("speaker", speaker_id, seed))
    return {
        "speaker_id": speaker_id,
        "embed_version": SPEAKER_EMBED_VERSION,
        "f0_base": round(float(100 + rng.rand() * 120), 6),
        "formants": [round(float(v), 6) for v in
                     (300 + rng.rand() * 400, 900 + rng.rand() * 800,
                      2200 + rng.rand() * 1000)],
        "energy_base": round(float(0.5 + rng.rand() * 0.5), 6),
    }


def normalize_text(text):
    """文本归一化：小写、压缩空白、去标点。"""
    out = " ".join(text.lower().split())
    return "".join(ch for ch in out if ch.isalnum() or ch == " ")


def segment_phonemes(normalized):
    """音素切分：字符 -> 音素符号序列（词间插入静音 SIL）。"""
    phones = []
    for ch in normalized:
        if ch == " ":
            phones.append("SIL")
        else:
            phones.append(_PHONEME_MAP.get(ch, "AH"))
    return phones


def acoustic_features(phonemes, spk, style_tag, prosody_scale,
                      duration_scale, duration_curve, precision, seed,
                      utterance_id):
    """声学特征生成：基频、能量包络与逐音素时长。"""
    rng = np.random.RandomState(stable_seed("acoustic", utterance_id, seed))
    style_factor = _STYLE_FACTOR.get(style_tag, 1.0)
    n = len(phonemes)
    base_dur = np.where(np.array(phonemes) == "SIL", 0.06, 0.09)
    jitter = 1.0 + (rng.rand(n) - 0.5) * 0.1
    durations = base_dur * jitter * duration_scale
    if duration_curve:
        curve = np.array(duration_curve, dtype=np.float64)
        idx = np.linspace(0, len(curve) - 1, n).round().astype(int)
        durations = durations * curve[idx]
    f0 = spk["f0_base"] * prosody_scale * style_factor * (
        1.0 + (rng.rand(n) - 0.5) * 0.08)
    energy = spk["energy_base"] * style_factor * (
        0.8 + rng.rand(n) * 0.4)
    if precision == "fp16":
        f0 = f0.astype(np.float16).astype(np.float64)
        energy = energy.astype(np.float16).astype(np.float64)
        durations = durations.astype(np.float16).astype(np.float64)
    return {
        "phonemes": list(phonemes),
        "durations": [round(float(v), 6) for v in durations],
        "f0": [round(float(v), 6) for v in f0],
        "energy": [round(float(v), 6) for v in energy],
    }


def vocoder_synthesize(acoustic, spk, vocoder_params, precision, seed,
                       utterance_id, sample_rate=SAMPLE_RATE):
    """声码器：谐波源 + 共振峰整形 + 能量包络，输出 int16 PCM。"""
    rng = np.random.RandomState(stable_seed("vocoder", utterance_id, seed))
    n_fft = int(vocoder_params.get("n_fft", 512))
    hop = int(vocoder_params.get("hop", 128))
    formants = spk["formants"]
    chunks = []
    phase = 0.0
    for f0, energy, dur in zip(acoustic["f0"], acoustic["energy"],
                               acoustic["durations"]):
        n = max(1, int(round(dur * sample_rate)))
        t = np.arange(n, dtype=np.float64) / sample_rate
        phase_inc = 2 * np.pi * f0 / sample_rate
        phase = phase + phase_inc * np.arange(n)
        source = np.zeros(n, dtype=np.float64)
        for h in range(1, 5):
            source += (1.0 / h) * np.sin(h * phase)
        # 共振峰整形：确定性加权正弦染色（系数来自 n_fft/hop，保证参数版本敏感）
        shape = np.zeros(n, dtype=np.float64)
        for k, fmt in enumerate(formants):
            w = (n_fft / 512.0) / (k + 1)
            shape += w * np.sin(2 * np.pi * fmt * t + k)
        noise = (rng.rand(n) - 0.5) * 0.02 * (hop / 128.0)
        seg = (source + 0.3 * shape + noise) * energy
        # 段边界 5ms 淡入淡出，保证拼接连续
        fade = min(n // 2, int(0.005 * sample_rate))
        if fade > 0:
            ramp = np.linspace(0.0, 1.0, fade)
            seg[:fade] *= ramp
            seg[-fade:] *= ramp[::-1]
        chunks.append(seg)
        phase = float(phase[-1]) if n else phase
    wave = np.concatenate(chunks) if chunks else np.zeros(1, dtype=np.float64)
    if precision == "fp16":
        wave = wave.astype(np.float16).astype(np.float64)
    peak = np.max(np.abs(wave)) or 1.0
    wave = wave / max(peak, 1.0) * 0.8
    return (wave * 32767).astype("<i2").tobytes()


def frame_features(pcm_bytes, speaker_id, acoustic, sample_rate=SAMPLE_RATE):
    """逐帧对齐：20ms 帧，输出 frame_index/speaker_id/f0_true/energy_true。"""
    pcm = np.frombuffer(pcm_bytes, dtype="<i2").astype(np.float64) / 32767.0
    n_frames = max(1, len(pcm) // FRAME_LEN)
    # 由声学轮廓推导逐帧真值 f0
    durations = np.array(acoustic["durations"])
    f0_per_phone = np.array(acoustic["f0"])
    boundaries = np.concatenate([[0.0], np.cumsum(durations)])
    total = boundaries[-1]
    frames = []
    for i in range(n_frames):
        t_mid = (i + 0.5) * FRAME_LEN / sample_rate
        pos = np.searchsorted(boundaries, t_mid, side="right") - 1
        pos = min(max(pos, 0), len(f0_per_phone) - 1)
        seg = pcm[i * FRAME_LEN:(i + 1) * FRAME_LEN]
        energy = float(np.sqrt(np.mean(seg ** 2))) if len(seg) else 0.0
        frames.append({
            "frame_index": i,
            "speaker_id": speaker_id,
            "f0_true": round(float(f0_per_phone[pos]), 6),
            "energy_true": round(energy, 6),
        })
    return frames


def prosody_check(frames, acoustic, tolerance=0.35):
    """韵律校验：帧能量与声学包络一致性检查。"""
    if not frames:
        return {"ok": False, "reason": "no_frames"}
    energies = np.array([f["energy_true"] for f in frames])
    mean_env = float(np.mean(acoustic["energy"])) or 1.0
    ratio = float(np.mean(energies)) / mean_env if mean_env else 0.0
    return {"ok": bool(ratio > 0.0 and abs(ratio - 0.5) < tolerance),
            "energy_ratio": round(ratio, 6)}
