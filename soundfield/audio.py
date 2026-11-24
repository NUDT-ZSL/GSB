"""WAV 读写，仅使用 Python 标准库 ``wave``，不引入任何音频库。"""

from __future__ import annotations

import os
import wave

import numpy as np

from . import errors as E


def read_mono_wav(path: str, expected_rate: int) -> np.ndarray:
    """读取单声道 16bit PCM WAV，返回 float64、幅度位于 [-1, 1)。"""

    try:
        with wave.open(path, "rb") as wf:
            n_channels = wf.getnchannels()
            sampwidth = wf.getsampwidth()
            frame_rate = wf.getframerate()
            n_frames = wf.getnframes()
            raw = wf.readframes(n_frames)
    except (OSError, wave.Error) as exc:
        raise E.BusinessError(E.E_WAV, f"WAV 无法读取: {path}", {"path": str(exc)}) from exc
    if n_channels != 1:
        raise E.BusinessError(E.E_WAV, "干信号 WAV 必须是单声道", {"path": path, "channels": n_channels})
    if sampwidth != 2:
        raise E.BusinessError(E.E_WAV, "干信号 WAV 必须是 16bit PCM", {"path": path, "sampwidth": sampwidth})
    if frame_rate != expected_rate:
        raise E.BusinessError(
            E.E_WAV,
            "干信号采样率与场景目标采样率不一致",
            {"path": path, "expected": expected_rate, "got": frame_rate},
        )
    if n_frames == 0:
        raise E.BusinessError(E.E_WAV, "干信号 WAV 为空", {"path": path})
    samples = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0
    return samples


def write_multichannel_wav(path: str, pcm_int16: np.ndarray, sample_rate: int) -> None:
    """写多声道 16bit PCM WAV。

    ``pcm_int16`` 形状为 ``(n_frames, n_channels)``，调用方负责原子替换语义。
    """

    if pcm_int16.ndim != 2 or pcm_int16.dtype != np.dtype("<i2"):
        raise E.BusinessError(E.E_OUTPUT, "WAV 写入需要 (帧, 声道) 的 int16 数组")
    n_frames, n_channels = pcm_int16.shape
    try:
        with wave.open(path, "wb") as wf:
            wf.setnchannels(n_channels)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(pcm_int16.tobytes(order="C"))
    except OSError as exc:
        raise E.BusinessError(E.E_OUTPUT, f"WAV 写入失败: {path}", {"path": str(exc)}) from exc


def read_wav_summary(path: str) -> dict[str, int] | None:
    """读取已有 WAV 的声道数/采样率/帧数；文件不存在或损坏时返回 None。"""

    if not os.path.exists(path):
        return None
    try:
        with wave.open(path, "rb") as wf:
            return {
                "channels": wf.getnchannels(),
                "sample_rate": wf.getframerate(),
                "frames": wf.getnframes(),
                "sampwidth": wf.getsampwidth(),
            }
    except (OSError, wave.Error):
        return None
