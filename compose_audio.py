#!/usr/bin/env python3
"""按编辑清单合成单声道播客音轨。

用法：
    python compose_audio.py <清单路径> <输出目录>

输入：JSON 数组，每项为
    {"path": "片段.wav", "start": 0.0, "fade_in": 0.0, "fade_out": 0.0}
片段须为单声道、16 位、8 kHz、PCM WAV。

输出：
    <输出目录>/output.wav   合成音轨（单声道 16 位 8 kHz PCM WAV）
    <输出目录>/report.json  处理报告

只使用 Python 标准库。输出不含随机数、时间戳或机器相关路径，
相同输入连续运行会得到字节级一致的结果。
"""

from __future__ import annotations

import argparse
import array
import json
import math
import os
import sys
import wave
from pathlib import Path
from typing import Any

SAMPLE_RATE = 8000
CHANNELS = 1
SAMPLE_WIDTH = 2  # 16 位
FRAME_BYTES = CHANNELS * SAMPLE_WIDTH
INT16_MAX = 32767
# 时间轴上限，防止异常大的 start 导致巨量内存分配；正常播客片段远小于此值。
MAX_TIMELINE_SECONDS = 3600
STATUS_OK = "完整"
STATUS_MISSING = "缺失"
STATUS_UNSUPPORTED = "格式不支持"
STATUS_READ_ERROR = "读取失败"


class ManifestReadError(Exception):
    """清单文件不存在、不可读或不是合法 JSON。"""


class ManifestFormatError(Exception):
    """JSON 合法，但不满足清单结构约束。"""


def _is_number(value: Any) -> bool:
    # bool 是 int 的子类，但 JSON 里的 true/false 不应被当成数字。
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def load_manifest(manifest_path: Path) -> tuple[list[dict[str, Any]], Path]:
    """读取并校验清单，返回归一化条目及清单所在目录。"""
    try:
        raw_text = manifest_path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise ManifestReadError(str(exc)) from exc

    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ManifestReadError(str(exc)) from exc

    if not isinstance(data, list):
        raise ManifestFormatError("清单顶层必须是 JSON 数组")

    entries: list[dict[str, Any]] = []
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            raise ManifestFormatError(f"第 {index + 1} 项不是 JSON 对象")
        if "path" not in item:
            raise ManifestFormatError(f"第 {index + 1} 项缺少 path")
        if not isinstance(item["path"], str):
            raise ManifestFormatError(f"第 {index + 1} 项的 path 必须是字符串")

        for field in ("start", "fade_in", "fade_out"):
            if field not in item:
                raise ManifestFormatError(f"第 {index + 1} 项缺少 {field}")
            value = item[field]
            if not _is_number(value) or not math.isfinite(value) or value < 0:
                raise ManifestFormatError(
                    f"第 {index + 1} 项的 {field} 必须是非负有限数值"
                )

        if item["start"] > MAX_TIMELINE_SECONDS:
            raise ManifestFormatError(
                f"第 {index + 1} 项的 start 超过 {MAX_TIMELINE_SECONDS} 秒上限"
            )

        entries.append(
            {
                "path": item["path"],
                "start": float(item["start"]),
                "fade_in": float(item["fade_in"]),
                "fade_out": float(item["fade_out"]),
            }
        )

    return entries, manifest_path.resolve().parent


def resolve_clip_path(raw_path: str, manifest_dir: Path) -> Path:
    """解析片段路径：绝对路径原样使用，相对路径按清单所在目录解析。

    始终相对清单目录解析（而非当前工作目录），可保证同一清单
    在不同工作目录下运行时结果一致。
    """
    candidate = Path(raw_path)
    if candidate.is_absolute():
        return candidate
    return manifest_dir / candidate


def read_clip(path: Path) -> tuple[array.array | None, str | None, str | None]:
    """读取一个片段。

    返回 (采样数组, 状态, 错误信息)。成功时状态为 STATUS_OK，
    失败时采样数组为 None。任何单片段问题都不抛出到调用方。
    """
    if not os.path.exists(path):
        return None, STATUS_MISSING, "文件不存在"
    if not os.path.isfile(path):
        return None, STATUS_READ_ERROR, "文件无法读取"

    try:
        file_size = os.path.getsize(path)
        with wave.open(str(path), "rb") as wav:
            channels = wav.getnchannels()
            sample_width = wav.getsampwidth()
            frame_rate = wav.getframerate()
            comptype = wav.getcomptype()
            frame_count = wav.getnframes()

            if (
                channels != CHANNELS
                or sample_width != SAMPLE_WIDTH
                or frame_rate != SAMPLE_RATE
                or comptype != "NONE"
            ):
                return None, STATUS_UNSUPPORTED, "非单声道 16 位 8kHz PCM"

            # 防止损坏头部声明的超大数据块导致巨量内存分配。
            if frame_count * FRAME_BYTES > file_size:
                return None, STATUS_READ_ERROR, "文件无法读取"

            raw_frames = wav.readframes(frame_count)
    except Exception:
        return None, STATUS_READ_ERROR, "文件无法读取"

    if len(raw_frames) != frame_count * FRAME_BYTES:
        return None, STATUS_READ_ERROR, "文件无法读取"
    if len(raw_frames) % FRAME_BYTES != 0:
        return None, STATUS_READ_ERROR, "文件无法读取"

    samples = array.array("h")
    samples.frombytes(raw_frames)
    if sys.byteorder == "big":
        samples.byteswap()
    return samples, STATUS_OK, None


def _fade_gain(
    index: int,
    length: int,
    fade_in_samples: int,
    fade_out_samples: int,
) -> float:
    """返回片段第 index 个采样上的线性淡入/淡出增益。

    淡入从片段起始处开始，淡出在片段结尾处归零；
    时长超过片段长度时，在调用前已按片段长度截断。
    """
    gain = 1.0
    if fade_in_samples > 0 and index < fade_in_samples:
        gain *= index / fade_in_samples
    if fade_out_samples > 0 and index >= length - fade_out_samples:
        gain *= (length - 1 - index) / fade_out_samples
    return gain


def _fade_sample_count(fade_seconds: float, length: int) -> int:
    if fade_seconds <= 0 or length <= 0:
        return 0
    # 超过片段总长时按片段长度截断，不报错。
    return min(int(round(fade_seconds * SAMPLE_RATE)), length)


def build_timeline(
    entries: list[dict[str, Any]], manifest_dir: Path
) -> tuple[list[str], list[str | None], int, list[float] | None, float]:
    """按清单顺序读取片段并混入时间轴。

    返回 (状态列表, 错误信息列表, 输出采样总数, 混音浮点数组, 峰值缩放系数)。
    无有效片段时混音数组为 None、总采样数为 0。
    """
    statuses: list[str] = []
    errors: list[str | None] = []
    pending: list[tuple[int, array.array, int, int]] = []
    total_samples = 0

    for entry in entries:
        path = resolve_clip_path(entry["path"], manifest_dir)
        samples, status, error = read_clip(path)
        statuses.append(status)
        errors.append(error)
        if samples is None:
            continue

        start_sample = int(round(entry["start"] * SAMPLE_RATE))
        length = len(samples)
        total_samples = max(total_samples, start_sample + length)
        pending.append(
            (
                start_sample,
                samples,
                _fade_sample_count(entry["fade_in"], length),
                _fade_sample_count(entry["fade_out"], length),
            )
        )

    if total_samples == 0:
        return statuses, errors, 0, None, 1.0

    # 以浮点累加，按清单顺序混合；之后再统一做峰值归一化，
    # 避免 int16 直接相加溢出，也保证同 start 的片段混合不过载。
    mix = [0.0] * total_samples
    for start_sample, samples, fade_in_samples, fade_out_samples in pending:
        length = len(samples)
        for index in range(length):
            gain = _fade_gain(index, length, fade_in_samples, fade_out_samples)
            mix[start_sample + index] += samples[index] * gain

    peak = max(abs(value) for value in mix)
    # 满幅缩放：峰值本就在 int16 范围内时不放大（保持原始电平），
    # 仅在过载时把整体线性压回满幅，避免硬削波。
    gain_scale = 1.0 if peak <= INT16_MAX else INT16_MAX / peak
    return statuses, errors, total_samples, mix, gain_scale


def write_output_wav(path: Path, mix: list[float] | None, gain_scale: float) -> None:
    """写入单声道 16 位 8 kHz PCM WAV；零采样时是标准 44 字节文件。"""
    if not mix:
        pcm_bytes = b""
    else:
        samples = array.array("h")
        for value in mix:
            scaled = int(round(value * gain_scale))
            scaled = max(-INT16_MAX - 1, min(INT16_MAX, scaled))
            samples.append(scaled)
        if sys.byteorder == "big":
            samples.byteswap()
        pcm_bytes = samples.tobytes()

    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(CHANNELS)
        wav.setsampwidth(SAMPLE_WIDTH)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(pcm_bytes)


def make_report(
    entries: list[dict[str, Any]],
    statuses: list[str],
    errors: list[str | None],
    total_samples: int,
) -> dict[str, Any]:
    clips = [
        {
            "path": entry["path"],
            "状态": status,
            "错误信息": error,
        }
        for entry, status, error in zip(entries, statuses, errors)
    ]
    return {
        "完整": all(status == STATUS_OK for status in statuses),
        "总时长": round(total_samples / SAMPLE_RATE, 6),
        "片段": clips,
    }


def write_report(path: Path, report: dict[str, Any]) -> None:
    text = json.dumps(report, ensure_ascii=False, indent=2)
    # 显式使用 LF，避免平台换行差异；固定键顺序与缩进保证字节级稳定。
    path.write_text(text + "\n", encoding="utf-8", newline="\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="按 JSON 编辑清单合成单声道播客音轨。"
    )
    parser.add_argument("manifest", help="编辑清单 JSON 路径")
    parser.add_argument("output_dir", help="输出目录（存放 output.wav 与 report.json）")
    args = parser.parse_args(argv)

    manifest_path = Path(args.manifest)
    output_dir = Path(args.output_dir)

    try:
        entries, manifest_dir = load_manifest(manifest_path)
    except ManifestReadError as exc:
        print(f"无法读取清单: {exc}", file=sys.stderr)
        return 2
    except ManifestFormatError as exc:
        print(f"清单格式错误: {exc}", file=sys.stderr)
        return 2

    try:
        statuses, errors, total_samples, mix, gain_scale = build_timeline(
            entries, manifest_dir
        )
        output_dir.mkdir(parents=True, exist_ok=True)
        write_output_wav(output_dir / "output.wav", mix, gain_scale)
        report = make_report(entries, statuses, errors, total_samples)
        write_report(output_dir / "report.json", report)
    except OSError as exc:
        print(f"无法写入输出: {exc}", file=sys.stderr)
        return 3

    ok_count = sum(1 for status in statuses if status == STATUS_OK)
    print(
        f"已生成 {output_dir / 'output.wav'} 与 {output_dir / 'report.json'}"
        f"（有效片段 {ok_count}/{len(statuses)}，总时长 {report['总时长']} 秒）"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
