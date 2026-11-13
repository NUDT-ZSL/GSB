#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""会议发言区间拼接工具（纯标准库）。

从两条单声道 16 位 PCM WAV 轨中，按 JSON 描述的时间区间提取发言，
按区间列表顺序拼接，生成 out.wav 与 out_report.json。

用法：
    python audio_merge.py TRACK0 TRACK1 INTERVALS_JSON OUT_DIR
    python audio_merge.py --track0 a.wav --track1 b.wav \
        --intervals ranges.json --out-dir out

区间 JSON：
    [
      {"track": 0, "start": 0.25, "end": 0.95, "fade": 0.05},
      {"track": 1, "start": 0.10, "end": 0.60}
    ]
也可以写成 {"intervals": [...]}。track/start/end 为必要字段，
fade 缺省为 0.01 秒。字段名兼容若干常见别名（见 README）。

退出码：
    0  成功（条目级错误会写入报告，不中断）
    2  区间 JSON 无法解析、缺少必要字段、命令行参数错误
    1  输出文件写入失败等运行期错误
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import wave
from array import array
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

DEFAULT_FADE_SECONDS = 0.01
FALLBACK_SAMPLE_RATE = 16_000
INT16_MIN = -32768
INT16_MAX = 32767
OUT_WAV_NAME = "out.wav"
OUT_REPORT_NAME = "out_report.json"

# 字段别名（统一小写比较；中文别名不受影响）
TRACK_KEYS = ("track", "track_index", "track_id", "track_no", "trackno",
              "channel", "轨号", "轨道")
START_KEYS = ("start", "start_seconds", "start_sec", "start_time", "begin",
              "from", "起始", "起点")
END_KEYS = ("end", "end_seconds", "end_sec", "end_time", "stop", "to",
            "结束", "终点")
FADE_KEYS = ("fade", "fade_seconds", "fade_sec", "fade_duration",
             "crossfade", "淡化")
ROOT_LIST_KEYS = ("intervals", "segments", "items", "clips", "ranges", "区间")

_MISSING = object()


class SchemaError(ValueError):
    """区间 JSON 结构错误；属于致命错误。"""


class TrackReadError(RuntimeError):
    """某条音轨不可读；属于条目级错误。"""


@dataclass
class Track:
    index: int
    path: str
    rate: int
    samples: array
    duration: float


# ---------------------------------------------------------------------------
# JSON 解析与结构校验
# ---------------------------------------------------------------------------

def _lookup(mapping: dict, names: Iterable[str]) -> Any:
    """按别名取值；键名大小写不敏感，找不到返回 _MISSING。"""
    lowered = {str(k).strip().lower(): v for k, v in mapping.items()}
    for name in names:
        if name.lower() in lowered:
            return lowered[name.lower()]
    return _MISSING


def _normalise_root(data: Any) -> list:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ROOT_LIST_KEYS:
            if key in data:
                value = data[key]
                if not isinstance(value, list):
                    raise SchemaError(f"根字段 '{key}' 必须是数组")
                return value
        # 允许根对象本身就是单个区间
        if (_lookup(data, TRACK_KEYS) is not _MISSING
                and _lookup(data, START_KEYS) is not _MISSING
                and _lookup(data, END_KEYS) is not _MISSING):
            return [data]
        raise SchemaError(
            "根对象缺少 intervals 数组（或 track/start/end 单区间字段）")
    raise SchemaError("区间 JSON 根节点必须是数组或对象")


def load_intervals(path: str | Path) -> list[dict]:
    """读取并校验区间 JSON；结构错误抛 SchemaError。

    这里只校验“是否存在必要字段”，数值合法性留到逐条处理，
    以便把条目级错误写进报告而不是整体中断。
    """
    p = Path(path)
    try:
        raw = p.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise SchemaError(f"无法读取区间 JSON '{p}': {exc}") from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SchemaError(
            f"区间 JSON 解析失败: {exc.msg} (line {exc.lineno}, column {exc.colno})"
        ) from exc

    items = _normalise_root(data)
    entries: list[dict] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise SchemaError(f"第 {index} 个区间不是 JSON 对象")

        track = _lookup(item, TRACK_KEYS)
        if track is _MISSING:
            raise SchemaError(f"第 {index} 个区间缺少必要字段 'track'")
        start = _lookup(item, START_KEYS)
        if start is _MISSING:
            raise SchemaError(f"第 {index} 个区间缺少必要字段 'start'")
        end = _lookup(item, END_KEYS)
        if end is _MISSING:
            raise SchemaError(f"第 {index} 个区间缺少必要字段 'end'")
        fade = _lookup(item, FADE_KEYS)
        if fade is _MISSING:
            fade = DEFAULT_FADE_SECONDS

        entries.append({
            "track": track,
            "start": start,
            "end": end,
            "fade": fade,
        })
    return entries


# ---------------------------------------------------------------------------
# WAV 读取 / 写出
# ---------------------------------------------------------------------------

def _array_from_wav_bytes(raw: bytes) -> array:
    usable = len(raw) - (len(raw) % 2)
    samples = array("h")
    samples.frombytes(raw[:usable])
    if sys.byteorder == "big":
        samples.byteswap()
    return samples


def _array_to_wav_bytes(samples: array) -> bytes:
    if sys.byteorder == "big":
        copy = array("h", samples)
        copy.byteswap()
        return copy.tobytes()
    return samples.tobytes()


def read_track(index: int, path: str | Path) -> Track:
    """读取单声道 16 位 PCM WAV；任何不可用情形抛 TrackReadError。"""
    p = Path(path)
    if not p.exists():
        raise TrackReadError(f"文件不存在: '{p}'")
    if not p.is_file():
        raise TrackReadError(f"路径不是普通文件: '{p}'")
    try:
        with wave.open(str(p), "rb") as wf:
            channels = wf.getnchannels()
            sample_width = wf.getsampwidth()
            rate = wf.getframerate()
            comp_type = wf.getcomptype()
            frames = wf.getnframes()
            if comp_type != "NONE":
                raise TrackReadError(f"不是未压缩 PCM WAV（压缩类型 {comp_type}）")
            if channels != 1:
                raise TrackReadError(f"不是单声道（实际 {channels} 声道）")
            if sample_width != 2:
                raise TrackReadError(
                    f"不是 16 位样本（实际 {sample_width * 8} 位）")
            if rate <= 0:
                raise TrackReadError(f"采样率无效: {rate}")
            raw = wf.readframes(frames)
    except TrackReadError:
        raise
    except (OSError, EOFError, wave.Error) as exc:
        raise TrackReadError(f"WAV 打开/读取失败: {exc}") from exc

    samples = _array_from_wav_bytes(raw)
    duration = len(samples) / rate
    return Track(index=index, path=str(p), rate=rate,
                 samples=samples, duration=duration)


# ---------------------------------------------------------------------------
# 区间处理
# ---------------------------------------------------------------------------

def _as_finite_number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    return number


def _as_track_number(value: Any) -> Optional[int]:
    number = _as_finite_number(value)
    if number is None or not number.is_integer():
        return None
    track_no = int(number)
    if track_no not in (0, 1):
        return None
    return track_no


def extract_segment(track: Track, start: float, end: float,
                    out_rate: int) -> array:
    """按秒提取区间，并统一到 out_rate。

    同采样率时直接切片；不同采样率时做线性插值，保证输出时长按秒一致。
    """
    src = track.samples
    src_rate = track.rate
    if src_rate == out_rate:
        first = int(round(start * src_rate))
        last = int(round(end * src_rate))
        first = max(0, min(first, len(src)))
        last = max(0, min(last, len(src)))
        return array("h", src[first:last])

    count = int(round((end - start) * out_rate))
    if count <= 0 or not src:
        return array("h")
    step = (end - start) * src_rate / count
    base = start * src_rate
    last_index = len(src) - 1
    result = array("h")
    for n in range(count):
        position = base + n * step
        left = int(math.floor(position))
        fraction = position - left
        if left < 0:
            left, fraction = 0, 0.0
        if left >= last_index:
            value = float(src[last_index])
        else:
            value = src[left] * (1.0 - fraction) + src[left + 1] * fraction
        result.append(int(round(value)))
    return result


def apply_fade(samples: array, fade_frames: int) -> None:
    """对区间首尾做线性淡化。

    淡化段内幅度从端点 0 单调递增到中心端 1；两端淡化段互不重叠。
    """
    length = len(samples)
    frames = min(max(fade_frames, 0), length // 2)
    if frames <= 0:
        return
    if frames == 1:
        samples[0] = 0
        samples[-1] = 0
        return
    denominator = frames - 1
    for i in range(frames):
        gain = i / denominator
        samples[i] = int(round(samples[i] * gain))
        samples[length - 1 - i] = int(round(samples[length - 1 - i] * gain))


def limit_int16(samples: array) -> None:
    for i, value in enumerate(samples):
        if value > INT16_MAX:
            samples[i] = INT16_MAX
        elif value < INT16_MIN:
            samples[i] = INT16_MIN


def build_output(track_paths: Sequence[str],
                 entries: Sequence[dict]) -> tuple[array, int, list[str], list[str], int]:
    """返回 (PCM 样本, 输出采样率, warnings, errors, 拼接区间数)。"""
    warnings: list[str] = []
    errors: list[str] = []

    tracks: list[Optional[Track]] = []
    for index, path in enumerate(track_paths):
        try:
            tracks.append(read_track(index, path))
        except TrackReadError as exc:
            tracks.append(None)
            errors.append(
                f"轨 {index} 读取失败，已跳过引用该轨的区间: {exc}")

    # 输出格式优先跟随轨 0；轨 0 不可读时退回到第一条可读轨。
    format_track = next((track for track in tracks if track is not None), None)
    out_rate = format_track.rate if format_track is not None else FALLBACK_SAMPLE_RATE

    output = array("h")
    merged_count = 0

    for index, entry in enumerate(entries):
        track_no = _as_track_number(entry["track"])
        if track_no is None:
            errors.append(
                f"区间[{index}] 轨号无效（{entry['track']!r}），"
                f"应为 0 或 1；已跳过")
            continue

        track = tracks[track_no]
        if track is None:
            # 轨读取失败已在上面记入 errors，这里只跳过引用该轨的条目。
            continue

        start = _as_finite_number(entry["start"])
        end = _as_finite_number(entry["end"])
        if start is None or end is None:
            errors.append(
                f"区间[{index}] start/end 必须是有限数值"
                f"（start={entry['start']!r}, end={entry['end']!r}）；已跳过")
            continue
        if end <= start:
            errors.append(
                f"区间[{index}] 起止时间无效"
                f"（start={start:g}s, end={end:g}s，时长不为正）；已跳过")
            continue

        duration = track.duration
        if start >= duration or end <= 0:
            errors.append(
                f"区间[{index}] 完全超出轨 {track_no} 的有效范围 "
                f"[0, {duration:.3f}]s；已跳过")
            continue

        effective_start = start
        effective_end = end
        if effective_start < 0:
            warnings.append(
                f"区间[{index}] 起点 {start:g}s 小于 0，已截断到 0s")
            effective_start = 0.0
        if effective_end > duration:
            warnings.append(
                f"区间[{index}] 终点 {end:g}s 超过轨 {track_no} 时长 "
                f"{duration:.3f}s，已截断到 {duration:.3f}s")
            effective_end = duration
        if effective_end <= effective_start:
            errors.append(
                f"区间[{index}] 截断后无可提取范围；已跳过")
            continue

        fade = _as_finite_number(entry["fade"])
        if fade is None:
            errors.append(
                f"区间[{index}] fade 必须是有限数值"
                f"（fade={entry['fade']!r}）；已跳过")
            continue
        if fade < 0:
            fade = 0.0

        interval_length = effective_end - effective_start
        # 淡化不能超过区间时长的一半；超过时按一半执行。
        fade = min(fade, interval_length / 2.0)

        segment = extract_segment(
            track, effective_start, effective_end, out_rate)
        if not segment:
            errors.append(
                f"区间[{index}] 截断后不足一个采样点；已跳过")
            continue

        apply_fade(segment, int(round(fade * out_rate)))
        limit_int16(segment)
        output.extend(segment)
        merged_count += 1

    return output, out_rate, warnings, errors, merged_count


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------

def write_outputs(out_dir: str | Path, output: array, rate: int,
                  warnings: Sequence[str], errors: Sequence[str],
                  merged_count: int) -> tuple[Path, Path, dict]:
    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    wav_path = directory / OUT_WAV_NAME
    report_path = directory / OUT_REPORT_NAME

    with wave.open(str(wav_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.setcomptype("NONE", "NONE")
        wf.writeframes(_array_to_wav_bytes(output))

    report = {
        "output_duration_seconds": round(len(output) / rate, 6),
        "merged_interval_count": merged_count,
        "warnings": list(warnings),
        "errors": list(errors),
        "sample_rate": rate,
        "bit_depth": 16,
        "channels": 1,
        "output_file": OUT_WAV_NAME,
    }
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    return wav_path, report_path, report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="audio_merge.py",
        description="按 JSON 区间从两条单声道 16 位 PCM WAV 轨拼接发言",
    )
    parser.add_argument(
        "paths", nargs="*", metavar="PATH",
        help="可选位置参数，依次为：轨0 WAV、轨1 WAV、区间 JSON、输出目录")
    parser.add_argument("--track0", "--track-0", dest="track0",
                        help="轨 0 的 WAV 路径")
    parser.add_argument("--track1", "--track-1", dest="track1",
                        help="轨 1 的 WAV 路径")
    parser.add_argument("--intervals", "--json", "-j", dest="intervals",
                        help="区间 JSON 路径")
    parser.add_argument("--out-dir", "--output-dir", "--out", "-o",
                        dest="out_dir", help="输出目录")
    return parser


def resolve_args(args: argparse.Namespace,
                 parser: argparse.ArgumentParser) -> tuple[str, str, str, str]:
    positional = list(args.paths)
    if len(positional) > 4:
        parser.error("位置参数最多 4 个：轨0 轨1 区间JSON 输出目录")

    values = [args.track0, args.track1, args.intervals, args.out_dir]
    labels = ["track0", "track1", "intervals", "out_dir"]
    for i, value in enumerate(values):
        if value is None and positional:
            values[i] = positional.pop(0)
    if positional:
        parser.error(
            f"存在多余的位置参数: {' '.join(positional)}"
            "（命名参数与位置参数混用时，位置参数按缺失项依次填充）")
    missing = [label for label, value in zip(labels, values) if value is None]
    if missing:
        parser.error(f"缺少参数: {', '.join(missing)}")
    return values[0], values[1], values[2], values[3]


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    track0, track1, intervals_path, out_dir = resolve_args(args, parser)

    # 先完成 JSON 结构校验；失败时不创建目录、不写任何输出文件。
    try:
        entries = load_intervals(intervals_path)
    except SchemaError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2

    output, rate, warnings, errors, merged_count = build_output(
        [track0, track1], entries)

    try:
        wav_path, report_path, report = write_outputs(
            out_dir, output, rate, warnings, errors, merged_count)
    except OSError as exc:
        print(f"错误: 输出写入失败: {exc}", file=sys.stderr)
        return 1

    print(f"已生成: {wav_path}")
    print(f"报告: {report_path}")
    print(f"输出时长: {report['output_duration_seconds']:.6f}s, "
          f"拼接区间数: {report['merged_interval_count']}, "
          f"warnings: {len(report['warnings'])}, errors: {len(report['errors'])}")
    for message in warnings:
        print(f"  warning: {message}")
    for message in errors:
        print(f"  error: {message}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
