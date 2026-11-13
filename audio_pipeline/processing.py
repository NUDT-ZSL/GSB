"""三个核心 DSP 步骤：静音裁剪、线性淡入淡出、增益（带溢出保护）。

处理中的样本统一用 float 表示，仅在最终写盘时量化回 16bit 整数，
避免多步骤累计量化误差。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .config import STEP_FADE, STEP_GAIN, STEP_TRIM
from .wav_io import WavData

# 16bit 有符号满幅
PEAK_MAX = 32767.0
PEAK_MIN = -32768.0
# 裁剪后每侧至少保留 10ms 静音
MIN_KEEP_SILENCE_MS = 10.0


def db_to_linear(db: float) -> float:
    """分贝转线性增益系数，例如 +6dB ≈ 2.0，-6dB ≈ 0.5。"""
    return 10.0 ** (db / 20.0)


def quantize_int16(samples: list[float]) -> list[int]:
    """四舍五入量化到有符号 16bit；调用方应保证已无溢出。"""
    return [max(-32768, min(32767, int(round(s)))) for s in samples]


@dataclass
class StepInfo:
    """单个处理步骤的可观测信息，汇总进运行报告。"""

    step: str
    detail: str


@dataclass
class TrimInfo(StepInfo):
    trimmed_leading: int   # 被裁掉的头部样本数
    trimmed_trailing: int  # 被裁掉的尾部样本数
    all_silence: bool      # 整段都低于阈值，未做裁剪

    def __init__(self, leading: int, trailing: int, all_silence: bool):
        super().__init__(
            STEP_TRIM,
            "整段低于阈值，保留原样"
            if all_silence
            else f"裁掉头部 {leading} 样本、尾部 {trailing} 样本",
        )
        self.trimmed_leading = leading
        self.trimmed_trailing = trailing
        self.all_silence = all_silence


@dataclass
class FadeInfo(StepInfo):
    fade_samples_per_side: int

    def __init__(self, fade_samples: int):
        if fade_samples <= 0:
            detail = "淡变时长为 0，未处理"
        else:
            detail = f"首尾各 {fade_samples} 样本线性淡入淡出"
        super().__init__(STEP_FADE, detail)
        self.fade_samples_per_side = fade_samples


@dataclass
class GainInfo(StepInfo):
    gain_db: float
    linear_factor: float
    limited: bool        # 是否触发了全局峰值保护
    limit_factor: float  # 保护时实际使用的附加压缩系数（1.0 表示未触发）

    def __init__(self, gain_db: float, limited: bool, limit_factor: float):
        factor = db_to_linear(gain_db) * limit_factor
        detail = (
            f"增益 {gain_db:+.1f}dB"
            + ("，已触发峰值保护防止削波" if limited else "")
        )
        super().__init__(STEP_GAIN, detail)
        self.gain_db = gain_db
        self.linear_factor = factor
        self.limited = limited
        self.limit_factor = limit_factor


def trim_silence(
    samples: list[float],
    sample_rate: int,
    threshold_db: float,
) -> tuple[list[float], TrimInfo]:
    """裁剪首尾低于阈值的静音，每侧至少保留 10ms。

    阈值按满幅换算：amplitude < 32767 * 10^(dB/20) 即视为静音。
    若整段都低于阈值，保留原样并在信息中标记，避免产出空音频。
    """
    threshold = PEAK_MAX * db_to_linear(threshold_db)
    n = len(samples)

    loud = [i for i, s in enumerate(samples) if abs(s) >= threshold]
    if not loud:
        return list(samples), TrimInfo(0, 0, all_silence=True)

    first_loud, last_loud = loud[0], loud[-1]

    # 每侧保留至少 10ms（向上取整，确保实际保留时长不少于 10ms）
    keep = math.ceil(sample_rate * MIN_KEEP_SILENCE_MS / 1000.0)
    start = max(0, first_loud - keep)
    end = min(n, last_loud + 1 + keep)

    trimmed = samples[start:end]
    return trimmed, TrimInfo(start, n - end, all_silence=False)


def apply_fade(
    samples: list[float],
    sample_rate: int,
    fade_ms: float,
) -> tuple[list[float], FadeInfo]:
    """首尾线性淡入淡出；fade_ms=0 时原样返回。"""
    out = list(samples)
    n = len(out)
    fade_len = int(round(sample_rate * fade_ms / 1000.0))
    # 淡入与淡出不能重叠
    fade_len = min(fade_len, n // 2)

    for i in range(fade_len):
        # 淡入：第 0 个样本系数 0，线性升至淡入段末端的 (N-1)/N
        fade_in_factor = i / fade_len if fade_len > 0 else 1.0
        # 淡出：淡出段起点系数 (N-1)/N，最后一个样本系数 0
        fade_out_index = n - fade_len + i
        fade_out_factor = (fade_len - 1 - i) / fade_len if fade_len > 0 else 1.0
        out[i] *= fade_in_factor
        out[fade_out_index] *= fade_out_factor

    return out, FadeInfo(fade_len)


def apply_gain(
    samples: list[float],
    gain_db: float,
) -> tuple[list[float], GainInfo]:
    """按 dB 增益调整音量，并保证绝不削波。

    先按配置系数放大；若结果超出 16bit 满幅，则对整段统一压缩到刚好
    不溢出（全局峰值保护，而非逐样本硬截断），并在报告中标记 limited。
    """
    factor = db_to_linear(gain_db)
    scaled = [s * factor for s in samples]

    peak = max((abs(s) for s in scaled), default=0.0)
    limited = False
    limit_factor = 1.0
    if peak > PEAK_MAX:
        limited = True
        # 留出极小余量，避免量化舍入把边界样本推到 32768
        limit_factor = PEAK_MAX / peak * 0.999999
        scaled = [s * limit_factor for s in scaled]

    return scaled, GainInfo(gain_db, limited, limit_factor)
