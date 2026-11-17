"""WAV 文件解析与封装（仅标准库 wave + NumPy，不使用任何外部音频库）。

支持 PCM 16bit（小端两字节）与 24bit（小端三字节），多声道按帧交织。
解码输出 float64 且归一化到 [-1, 1)；编码时按目标位深做对称量化。
"""

import os
import wave

import numpy as np

from .errors import FormatError, MissingError, WriteCommitError

ALLOWED_RATES = (44100, 48000, 96000)
ALLOWED_BITS = (16, 24)


class WavInfo:
    __slots__ = ("path", "sample_rate", "bits", "channels", "frames", "data_bytes")

    def __init__(self, path, sample_rate, bits, channels, frames, data_bytes):
        self.path = path
        self.sample_rate = sample_rate
        self.bits = bits
        self.channels = channels
        self.frames = frames
        self.data_bytes = data_bytes

    def as_dict(self):
        return {
            "path": self.path,
            "sample_rate": self.sample_rate,
            "bits": self.bits,
            "channels": self.channels,
            "frames": self.frames,
            "data_bytes": self.data_bytes,
        }


def read_header(path):
    """只读取文件头，返回 WavInfo，不把采样数据载入内存。"""
    if not os.path.isfile(path):
        raise MissingError("WAV 文件不存在", refs=[path])
    try:
        with wave.open(path, "rb") as wf:
            channels = wf.getnchannels()
            sample_width = wf.getsampwidth()
            sample_rate = wf.getframerate()
            frames = wf.getnframes()
            comp = wf.getcomptype()
    except (wave.Error, EOFError, OSError) as exc:
        raise FormatError("WAV 文件头无法解析", refs=[path], detail={"reason": str(exc)})
    if comp != "NONE":
        raise FormatError("仅支持未压缩 PCM WAV", refs=[path], detail={"comptype": comp})
    if sample_width not in (2, 3):
        raise FormatError("位深仅支持 16/24 bit", refs=[path],
                          detail={"sample_width_bytes": sample_width})
    bits = sample_width * 8
    data_bytes = frames * channels * sample_width
    return WavInfo(path, sample_rate, bits, channels, frames, data_bytes)


def decode_wav(path):
    """读取并解码整段 WAV，返回 (WavInfo, float64 ndarray[frames, channels])。"""
    info = read_header(path)
    with wave.open(path, "rb") as wf:
        raw = wf.readframes(info.frames)
    if info.bits == 16:
        flat = np.frombuffer(raw, dtype="<i2").astype(np.float64)
        flat /= 32768.0
    else:
        raw_bytes = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3)
        as32 = (
            raw_bytes[:, 0].astype(np.int32)
            | (raw_bytes[:, 1].astype(np.int32) << 8)
            | (raw_bytes[:, 2].astype(np.int32) << 16)
        )
        negative = as32 & 0x800000
        as32 = np.where(negative != 0, as32 - 0x1000000, as32)
        flat = as32.astype(np.float64) / 8388608.0
    samples = flat.reshape(info.frames, info.channels)
    return info, samples


def encode_pcm(samples, bits):
    """把 float64 ndarray[frames, channels] 量化为交织 PCM 字节串。"""
    clipped = np.clip(samples, -1.0, 1.0)
    if bits == 16:
        q = np.round(clipped * 32767.0).astype("<i2")
        return q.tobytes()
    if bits == 24:
        q = np.round(clipped * 8388607.0).astype(np.int32)
        flat = q.reshape(-1)
        out = np.empty((flat.shape[0], 3), dtype=np.uint8)
        out[:, 0] = flat & 0xFF
        out[:, 1] = (flat >> 8) & 0xFF
        out[:, 2] = (flat >> 16) & 0xFF
        return out.tobytes()
    raise FormatError("目标位深仅支持 16/24 bit", detail={"bits": bits})


def write_wav(path, samples, sample_rate, bits):
    """原子友好的写法：调用方负责临时文件路径与后续替换。"""
    if samples.ndim == 1:
        samples = samples[:, np.newaxis]
    channels = samples.shape[1]
    pcm = encode_pcm(samples, bits)
    try:
        with wave.open(path, "wb") as wf:
            wf.setnchannels(channels)
            wf.setsampwidth(bits // 8)
            wf.setframerate(sample_rate)
            wf.writeframes(pcm)
    except (wave.Error, OSError) as exc:
        raise WriteCommitError("WAV 临时文件写入失败", refs=[path],
                               detail={"reason": str(exc)})
    return read_header(path)

