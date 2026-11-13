"""WAV 读写：仅支持单声道 16-bit PCM，基于标准库 ``wave`` / ``array``。

读入的样本统一表示为 :class:`array.array('h')`（有符号 16-bit 小端机器原生序），
写出时同样按 16-bit PCM 落盘，保证输出可被标准库 ``wave`` 直接读取。
"""

from __future__ import annotations

import array
import os
import wave

from .errors import EditError, ErrorCode

#: 16-bit PCM 样本幅度上下限
SAMPLE_MIN = -(2 ** 15)       # -32768
SAMPLE_MAX = 2 ** 15 - 1      #  32767


class WavData:
    """内存中的单声道波形。

    :param samples: 有符号 16-bit 样本序列
    :param sample_rate: 采样率（Hz）
    """

    __slots__ = ("samples", "sample_rate")

    def __init__(self, samples: array.array, sample_rate: int):
        self.samples = samples
        self.sample_rate = sample_rate

    @property
    def frame_count(self) -> int:
        return len(self.samples)

    @property
    def duration_seconds(self) -> float:
        return len(self.samples) / self.sample_rate


def load_wav(path: str) -> WavData:
    """读取并校验一个 WAV 文件。

    校验规则（任一不满足即抛 :class:`EditError`，调用方据此跳过该来源）：
    1. 文件必须存在且可读（不存在 -> SOURCE_NOT_FOUND，IO 失败 -> SOURCE_NOT_READABLE）；
    2. 必须是可解析的 WAV 容器；
    3. 必须为单声道；
    4. 必须为 16-bit PCM（sampwidth == 2）。

    采样率本身在此处不做全局一致性判断——由引擎结合时间线已有的采样率校验，
    以便给出准确的 SAMPLE_RATE_MISMATCH 错误。
    """
    if not os.path.exists(path):
        raise EditError(
            ErrorCode.SOURCE_NOT_FOUND,
            f"来源文件不存在: {path}",
            retryable=True,
        )

    try:
        wf = wave.open(path, "rb")
    except (wave.Error, EOFError, OSError) as exc:
        # 文件存在但暂时不可读/损坏：归类为临时性错误，允许恢复后续跑
        raise EditError(
            ErrorCode.SOURCE_NOT_READABLE,
            f"来源文件暂时不可读或不是合法 WAV: {path} ({exc})",
            retryable=True,
        ) from exc

    try:
        channels = wf.getnchannels()
        sample_width = wf.getsampwidth()
        sample_rate = wf.getframerate()
        frame_count = wf.getnframes()
        comp_type = wf.getcomptype()
        try:
            raw = wf.readframes(frame_count)
        except (wave.Error, OSError) as exc:
            raise EditError(
                ErrorCode.SOURCE_NOT_READABLE,
                f"来源文件样本数据读取失败: {path} ({exc})",
                retryable=True,
            ) from exc
    finally:
        wf.close()

    problems = []
    if comp_type != "NONE":
        problems.append(f"非 PCM 压缩格式(comptype={comp_type})")
    if channels != 1:
        problems.append(f"期望单声道，实际 {channels} 声道")
    if sample_width != 2:
        problems.append(f"期望 16-bit PCM(2 字节/样本)，实际 {sample_width * 8}-bit")
    if problems:
        raise EditError(
            ErrorCode.INVALID_WAV_FORMAT,
            f"来源文件格式不合法: {path}；{'; '.join(problems)}",
            retryable=False,
        )

    samples = array.array("h")
    try:
        samples.frombytes(raw)
    except ValueError as exc:
        raise EditError(
            ErrorCode.INVALID_WAV_FORMAT,
            f"来源文件样本数据损坏，无法按 16-bit 解析: {path} ({exc})",
            retryable=False,
        ) from exc

    # 处理大端机器：array 按原生序解释，wave 规定 PCM 为小端
    if sys_byteorder_big():  # pragma: no cover - 当前验证环境均为小端
        samples.byteswap()

    return WavData(samples=samples, sample_rate=sample_rate)


def write_wav(path: str, samples: array.array, sample_rate: int) -> None:
    """将 16-bit 单声道样本写成 WAV（原子写：先写临时文件再替换）。"""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    tmp_path = os.path.join(directory, f".{os.path.basename(path)}.tmp")
    with wave.open(tmp_path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(samples.tobytes())
    os.replace(tmp_path, path)


def sys_byteorder_big() -> bool:
    import sys

    return sys.byteorder == "big"
