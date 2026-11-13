"""WAV 读写：只支持单声道、16bit PCM、采样率 8000–48000Hz。

参数（采样率/位深/声道）原样写入输出文件，不做重采样或位深转换。
"""

from __future__ import annotations

import struct
import wave
from dataclasses import dataclass
from pathlib import Path

# wave 模块中表示未压缩 PCM 的格式标记
WAVE_FORMAT_PCM = 1
SAMPLE_WIDTH_BYTES = 2
SAMPLE_WIDTH_BITS = 16
CHANNELS_MONO = 1
MIN_SAMPLE_RATE = 8000
MAX_SAMPLE_RATE = 48000


class WavFormatError(ValueError):
    """文件不是受支持的 WAV（非 WAV / 非 PCM / 位深或声道或采样率不符 / 数据损坏）。"""


@dataclass(frozen=True)
class WavData:
    """读入内存的一段单声道 16bit PCM 音频。

    samples 使用有符号 16bit 整数表示，范围 [-32768, 32767]。
    """

    samples: list[int]
    sample_rate: int

    @property
    def num_samples(self) -> int:
        return len(self.samples)

    @property
    def duration_seconds(self) -> float:
        return len(self.samples) / self.sample_rate


def _describe(value: object) -> str:
    name = getattr(value, "name", None)
    return str(name or value)


def read_wav(path: str | Path) -> WavData:
    """读取并严格校验 WAV 文件，任何不符都抛 WavFormatError（不抛底层异常）。"""
    wav_path = Path(path)
    try:
        raw = wav_path.read_bytes()
    except OSError as exc:
        raise WavFormatError(f"文件无法读取：{exc}") from None

    # wave 模块靠 RIFF 头识别格式；非 WAV 文件在这里会直接报错。
    # 注意：Python 3.10 的 wave.open 不接受 pathlib.Path，必须传字符串。
    try:
        with wave.open(str(wav_path), "rb") as wf:
            n_channels = wf.getnchannels()
            sample_width = wf.getsampwidth()
            sample_rate = wf.getframerate()
            n_frames = wf.getnframes()
            comp_type = wf.getcomptype()
            comp_name = wf.getcompname()
            frames = wf.readframes(n_frames)
    except (wave.Error, EOFError, struct.error) as exc:
        raise WavFormatError(f"不是合法 WAV 文件或内容已损坏：{_describe(exc)}") from None

    if comp_type != "NONE":  # wave 对 PCM 返回 "NONE"
        raise WavFormatError(f"仅支持未压缩 PCM，实际压缩格式：{comp_type}（{comp_name}）")
    if n_channels != CHANNELS_MONO:
        raise WavFormatError(f"仅支持单声道，实际声道数：{n_channels}")
    if sample_width != SAMPLE_WIDTH_BYTES:
        raise WavFormatError(
            f"仅支持 16bit PCM，实际位深：{sample_width * 8}bit"
        )
    if not (MIN_SAMPLE_RATE <= sample_rate <= MAX_SAMPLE_RATE):
        raise WavFormatError(
            f"采样率 {sample_rate}Hz 不在支持范围 {MIN_SAMPLE_RATE}–{MAX_SAMPLE_RATE}Hz 内"
        )

    expected_bytes = n_frames * SAMPLE_WIDTH_BYTES
    if len(frames) != expected_bytes:
        raise WavFormatError(
            f"音频数据长度与帧数不符（声明 {n_frames} 帧 / {expected_bytes} 字节，"
            f"实际 {len(frames)} 字节），文件可能已损坏"
        )

    try:
        samples = list(struct.unpack("<%dh" % n_frames, frames))
    except struct.error as exc:
        raise WavFormatError(f"音频样本解析失败，文件可能已损坏：{exc}") from None

    return WavData(samples=samples, sample_rate=sample_rate)


def write_wav(path: str | Path, data: WavData) -> None:
    """按读入时的采样率/位深/声道参数写出 WAV（自动创建父目录）。"""
    out_path = Path(path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # 再保险一次：量化后的样本必须落在 16bit 有符号范围内
    clamped = [max(-32768, min(32767, int(s))) for s in data.samples]
    payload = struct.pack("<%dh" % len(clamped), *clamped)

    with wave.open(str(out_path), "wb") as wf:
        wf.setnchannels(CHANNELS_MONO)
        wf.setsampwidth(SAMPLE_WIDTH_BYTES)
        wf.setframerate(data.sample_rate)
        wf.writeframes(payload)
