#!/usr/bin/env python3
"""过场动画多轨混音工具。

读取时间表 JSON，将对白 / 音效 / 环境等 WAV 分轨按入点（毫秒）放置到时间轴上，
按振幅相加混合（溢出削减而非回绕），输出 16-bit PCM 混音 WAV 与合成报告 JSON。

设计原则：单条分轨损坏（缺失、格式不符、入点非法）只影响该分轨本身，
其余分轨照常混音；时间表无法解析或目标格式参数非法时整体终止并返回非零退出码。

仅使用 Python 标准库，Python >= 3.10。
"""
from __future__ import annotations

import argparse
import json
import sys
import wave
from array import array
from pathlib import Path

SAMPLE_WIDTH_BYTES = 2          # 输出固定为 16-bit PCM
INT16_MIN, INT16_MAX = -32768, 32767
MIN_SAMPLE_RATE, MAX_SAMPLE_RATE = 8000, 192000
ALLOWED_CHANNELS = (1, 2)

EXIT_OK = 0
EXIT_FATAL = 2                  # 时间表无法解析 / 目标格式参数非法 / 输出无法写入


class FatalError(Exception):
    """致命错误：打印可读信息并以非零退出码终止。"""


def is_nonneg_int(value) -> bool:
    """非负整数（JSON 的 true/false 是 bool，须排除）。"""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="按时间表 JSON 将多条 WAV 分轨合成立体声（或单声道）母带。"
    )
    parser.add_argument("--timeline", required=True, help="时间表 JSON 路径")
    parser.add_argument("--out", required=True, help="输出混音 WAV 路径")
    parser.add_argument("--report", required=True, help="输出合成报告 JSON 路径")
    parser.add_argument("--sample-rate", type=int, default=44100,
                        help="目标采样率 Hz，范围 8000-192000，默认 44100")
    parser.add_argument("--channels", type=int, default=2,
                        help="目标声道数，1 或 2，默认 2（立体声）")
    args = parser.parse_args(argv)

    # 目标格式参数校验：非法即终止（核对项 7）
    if not (MIN_SAMPLE_RATE <= args.sample_rate <= MAX_SAMPLE_RATE):
        raise FatalError(
            f"目标采样率非法: {args.sample_rate}，允许范围 "
            f"{MIN_SAMPLE_RATE}-{MAX_SAMPLE_RATE} Hz"
        )
    if args.channels not in ALLOWED_CHANNELS:
        raise FatalError(
            f"目标声道数非法: {args.channels}，仅允许 {list(ALLOWED_CHANNELS)}"
        )
    return args


def load_timeline(path: Path) -> dict:
    """读取并校验时间表顶层结构；失败抛 FatalError（核对项 7）。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise FatalError(f"无法读取时间表文件 {path}: {exc}")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise FatalError(f"时间表 JSON 无法解析: {path}: {exc}")
    if not isinstance(data, dict):
        raise FatalError("时间表顶层必须是 JSON 对象")
    total = data.get("total_duration_ms")
    if not is_nonneg_int(total):
        raise FatalError("时间表缺少合法的 total_duration_ms（非负整数毫秒）")
    tracks = data.get("tracks")
    if not isinstance(tracks, list):
        raise FatalError("时间表缺少合法的 tracks 数组")
    return data


def read_wav(path: Path):
    """读取 WAV，返回 (frames_array('h'), n_frames, n_channels, rate, sampwidth)。

    文件无法读取时抛 OSError / wave.Error，由调用方记录并跳过（核对项 4）。
    """
    with wave.open(str(path), "rb") as wf:
        params = (wf.getnchannels(), wf.getframerate(), wf.getsampwidth(),
                  wf.getnframes(), wf.getcomptype())
        raw = wf.readframes(params[3])
    nch, rate, width, nframes, comptype = params
    if comptype != "NONE":
        raise wave.Error(f"不支持的压缩格式: {comptype}")
    samples = array("h")
    if width == SAMPLE_WIDTH_BYTES:
        samples.frombytes(raw[: nframes * nch * width])
        if sys.byteorder == "big":
            samples.byteswap()
    return samples, nframes, nch, rate, width


def mix(timeline_path: Path, out_path: Path, report_path: Path,
        target_rate: int, target_channels: int) -> dict:
    timeline = load_timeline(timeline_path)
    total_ms = timeline["total_duration_ms"]
    base_dir = timeline_path.resolve().parent

    invalid_entries = []     # 入点等字段非法的条目（核对项 1）
    unprocessed = []         # 已识别但未参与混音的分轨及原因（核对项 2/3/4）
    mixed = []               # 实际参与混音的分轨
    silent_ids = []          # 全静音分轨（核对项 9）

    declared_frames = round(total_ms * target_rate / 1000)
    content_end_frames = 0
    pending = []             # (offset_frames, samples) 待累加

    for index, entry in enumerate(timeline["tracks"]):
        if not isinstance(entry, dict):
            invalid_entries.append({"index": index, "entry": entry,
                                    "reason": "条目不是 JSON 对象"})
            continue
        track_id = entry.get("id") or entry.get("file") or f"#{index}"
        file_rel = entry.get("file")
        in_point = entry.get("in_point_ms")

        # 核对项 1：入点必须是非负整数毫秒，非法条目单独记录
        if not isinstance(file_rel, str) or not file_rel:
            invalid_entries.append({"index": index, "id": track_id,
                                    "reason": "缺少合法的 file 字段"})
            continue
        if not is_nonneg_int(in_point):
            invalid_entries.append({
                "index": index, "id": track_id, "file": file_rel,
                "reason": f"入点 in_point_ms 非法（须为非负整数毫秒）: {in_point!r}"})
            continue

        # 核对项 2：入点超过声明总时长的分轨不得混入
        if in_point > total_ms:
            unprocessed.append({
                "id": track_id, "file": file_rel, "in_point_ms": in_point,
                "reason": f"入点 {in_point} ms 超过声明总时长 {total_ms} ms，未混入"})
            continue

        # 核对项 4：文件缺失或无法读取不影响其余分轨
        wav_path = (base_dir / file_rel).resolve()
        if not wav_path.is_file():
            unprocessed.append({"id": track_id, "file": file_rel,
                                "reason": "分轨文件缺失"})
            continue
        try:
            samples, nframes, nch, rate, width = read_wav(wav_path)
        except (OSError, wave.Error, EOFError) as exc:
            unprocessed.append({"id": track_id, "file": file_rel,
                                "reason": f"分轨文件无法读取: {exc}"})
            continue

        # 核对项 3：采样率 / 声道数 / 位深与目标格式不一致时跳过
        if width != SAMPLE_WIDTH_BYTES:
            unprocessed.append({"id": track_id, "file": file_rel,
                                "reason": f"位深不符: 分轨 {width * 8} bit，"
                                          f"目标 {SAMPLE_WIDTH_BYTES * 8} bit"})
            continue
        if rate != target_rate:
            unprocessed.append({"id": track_id, "file": file_rel,
                                "reason": f"采样率不符: 分轨 {rate} Hz，目标 {target_rate} Hz"})
            continue
        if nch != target_channels:
            unprocessed.append({"id": track_id, "file": file_rel,
                                "reason": f"声道数不符: 分轨 {nch} 声道，目标 {target_channels} 声道"})
            continue

        offset_frames = round(in_point * target_rate / 1000)
        content_end_frames = max(content_end_frames, offset_frames + nframes)
        pending.append((offset_frames, samples))

        duration_ms = round(nframes * 1000 / rate)
        is_silent = not any(samples)
        if is_silent:
            silent_ids.append(track_id)   # 核对项 9：静音分轨允许混音，单独列出
        mixed.append({"id": track_id, "file": file_rel, "in_point_ms": in_point,
                      "duration_ms": duration_ms, "silent": is_silent})

    # 输出时长 = max(声明总时长, 实际内容末尾)，两者都如实写进报告（核对项 8）
    actual_ms = round(content_end_frames * 1000 / target_rate)
    out_frames = max(declared_frames, content_end_frames)

    # 核对项 5：32-bit 累加器按振幅相加，最后削减到 int16 范围（不回绕）
    acc = array("i", [0]) * (out_frames * target_channels)
    for offset_frames, samples in pending:
        start = offset_frames * target_channels
        for i, v in enumerate(samples):
            acc[start + i] += v
    clipped = array("h", ((INT16_MIN if v < INT16_MIN
                           else INT16_MAX if v > INT16_MAX else v)
                          for v in acc))

    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(out_path), "wb") as wf:
            wf.setnchannels(target_channels)
            wf.setsampwidth(SAMPLE_WIDTH_BYTES)
            wf.setframerate(target_rate)
            wf.writeframes(clipped.tobytes())
    except OSError as exc:
        raise FatalError(f"无法写入输出 WAV {out_path}: {exc}")

    report = {
        "timeline": str(timeline_path),
        "output": str(out_path),
        "target_format": {"sample_rate": target_rate,
                          "channels": target_channels,
                          "sample_width_bits": SAMPLE_WIDTH_BYTES * 8},
        "declared_total_ms": total_ms,
        "actual_total_ms": actual_ms,
        "duration_diff_ms": actual_ms - total_ms,
        "mixed_track_count": len(mixed),
        "mixed_tracks": mixed,
        "unprocessed_tracks": unprocessed,
        "invalid_entries": invalid_entries,
        "silent_tracks": silent_ids,
    }
    try:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                               encoding="utf-8")
    except OSError as exc:
        raise FatalError(f"无法写入报告 {report_path}: {exc}")
    return report


def main(argv=None) -> int:
    try:
        args = parse_args(argv)
        report = mix(Path(args.timeline), Path(args.out), Path(args.report),
                     args.sample_rate, args.channels)
    except FatalError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return EXIT_FATAL
    print(f"混音完成: {report['output']}")
    print(f"参与混音 {report['mixed_track_count']} 条，"
          f"未处理 {len(report['unprocessed_tracks'])} 条，"
          f"非法条目 {len(report['invalid_entries'])} 条，"
          f"静音 {len(report['silent_tracks'])} 条")
    print(f"声明总时长 {report['declared_total_ms']} ms，"
          f"实际合成 {report['actual_total_ms']} ms，"
          f"差值 {report['duration_diff_ms']} ms")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
