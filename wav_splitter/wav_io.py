"""WAV 读取、毫秒级采样点对齐与片段写出（仅支持未压缩单声道 PCM）。"""

from __future__ import annotations

import os
import wave

from decimal import Decimal, ROUND_HALF_UP

from .errors import SplitterError
from .models import WavInfo

# 允许的 PCM 位宽（字节 -> 位深）
_VALID_WIDTHS = (1, 2, 3, 4)
# 浮点比较容差：1 纳秒，吸收二进制浮点的表示噪声
_EPSILON = 1e-9


def load_wav(wav_path: str) -> tuple[WavInfo, bytes]:
    """读取并校验单声道 PCM WAV，返回 (头部信息, 全部 PCM 帧字节)。

    任何不符合“有效单声道 PCM”的情况都抛 :class:`SplitterError`。
    """
    if not os.path.exists(wav_path):
        raise SplitterError(f"WAV 文件不存在：{wav_path}")
    if os.path.isdir(wav_path):
        raise SplitterError(f"WAV 路径是目录而非文件：{wav_path}")

    try:
        with wave.open(wav_path, "rb") as wf:
            channels = wf.getnchannels()
            sample_width = wf.getsampwidth()
            frame_rate = wf.getframerate()
            frame_count = wf.getnframes()
            comptype = wf.getcomptype()
            compname = wf.getcompname()
            pcm = wf.readframes(frame_count)
    except wave.Error as exc:
        raise SplitterError(f"WAV 不是有效的单声道 PCM 文件：{wav_path}（{exc}）") from None
    except OSError as exc:
        raise SplitterError(f"WAV 文件无法读取：{wav_path}（{exc}）") from None

    if comptype != "NONE":
        raise SplitterError(
            f"WAV 不是未压缩 PCM 格式：{wav_path}（压缩类型 {comptype}/{compname}）"
        )
    if channels != 1:
        raise SplitterError(
            f"WAV 必须为单声道，实际为 {channels} 声道：{wav_path}"
        )
    if sample_width not in _VALID_WIDTHS:
        raise SplitterError(
            f"WAV 位深非法（采样宽度 {sample_width} 字节）：{wav_path}"
        )
    if frame_rate <= 0:
        raise SplitterError(f"WAV 采样率非法（{frame_rate} Hz）：{wav_path}")
    if frame_count <= 0:
        raise SplitterError(f"WAV 不包含任何音频帧：{wav_path}")

    info = WavInfo(
        path=wav_path,
        channels=channels,
        sample_width=sample_width,
        frame_rate=frame_rate,
        frame_count=frame_count,
        comptype=comptype,
        compname=compname,
    )
    return info, pcm


def time_to_sample_index(seconds: Decimal, frame_rate: int) -> int:
    """把秒数按“毫秒级精度、四舍五入（半值向上）”对齐到采样点索引。

    规则：先把秒量化到整毫秒（``ROUND_HALF_UP``），再乘以采样率并
    再次四舍五入到整数采样点。全程使用 Decimal，避免二进制浮点误差。

    例：1.2345 秒 @ 8000Hz -> 毫秒量化为 1.235 -> 1.235*8000 = 9880。
    """
    milliseconds = (seconds * 1000).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    index = (milliseconds * frame_rate / 1000).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP
    )
    return int(index)


def is_within_duration(start: Decimal, end: Decimal, info: WavInfo) -> bool:
    """判断区间两端是否都落在 [0, 总时长] 内（含端点，带纳秒级容差）。"""
    duration = info.duration_seconds
    start_f = float(start)
    end_f = float(end)
    return start_f >= -_EPSILON and end_f <= duration + _EPSILON


def write_segment(
    output_path: str,
    info: WavInfo,
    pcm: bytes,
    start_sample: int,
    end_sample: int,
) -> None:
    """从原始 PCM 字节中切片并写出 WAV。

    采样率、位深、声道数直接沿用原文件头部，保证三项参数完全一致。
    """
    width = info.sample_width
    chunk = pcm[start_sample * width:end_sample * width]
    try:
        with wave.open(output_path, "wb") as wf:
            wf.setnchannels(info.channels)
            wf.setsampwidth(info.sample_width)
            wf.setframerate(info.frame_rate)
            wf.writeframes(chunk)
    except OSError as exc:
        raise SplitterError(f"片段写出失败：{output_path}（{exc}）") from None
