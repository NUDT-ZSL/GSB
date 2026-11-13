# -*- coding: utf-8 -*-
"""
wav_merger.py — 从 PCM WAV 现场录音中按 JSON 标记区间提取片段并合并为新音轨。

仅使用 Python 标准库（wave / array / json / argparse 等）。
约束：
  * 仅接受 16-bit 整数 PCM WAV，声道数 1 或 2；
  * 标记区间单位为秒，可为小数，按采样率对齐到整样本；
  * 片段按标记顺序拼接，保留原始声道数与采样率，不混合声道；
  * 可选交叉淡化（毫秒），仅作用于片段连接处，不改变总样本数；
  * 输出结果 WAV 与 JSON 报告。

退出码：0 成功；1 输入校验或处理错误；2 命令行参数错误（argparse 默认）。
"""

from __future__ import annotations

import argparse
import array
import json
import os
import sys
import wave

# Windows 控制台/管道可能使用 GBK 代码页，统一 UTF-8 输出，保证中文信息可被正确捕获
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


class MergeError(Exception):
    """业务错误：错误信息直接展示给用户，进程以非零码退出。"""


def load_wav(path):
    """读取 WAV，校验格式，返回 (samples, channels, sample_rate, n_frames)。

    samples 为 array('h')，按帧交错存放（立体声时 L,R,L,R,...）。
    """
    if not os.path.isfile(path):
        raise MergeError("输入 WAV 文件不存在: {}".format(path))
    try:
        with wave.open(path, "rb") as wf:
            channels = wf.getnchannels()
            sample_width = wf.getsampwidth()
            sample_rate = wf.getframerate()
            n_frames = wf.getnframes()
            raw = wf.readframes(n_frames)
    except (wave.Error, EOFError, OSError) as exc:
        raise MergeError("无法读取 WAV 文件（可能不是有效的 PCM WAV）: {}；{}".format(path, exc))

    if sample_width != 2:
        raise MergeError(
            "不支持的样本格式: 样本宽度为 {} 字节（{} bit），本程序仅支持 16-bit 整数 PCM".format(
                sample_width, sample_width * 8
            )
        )
    if channels not in (1, 2):
        raise MergeError(
            "不支持的样本格式: 声道数为 {}，本程序仅支持单声道或立体声（1~2 声道）".format(channels)
        )
    if sample_rate <= 0:
        raise MergeError("不支持的样本格式: 采样率非法（{}）".format(sample_rate))

    samples = array.array("h")
    samples.frombytes(raw)
    if sys.byteorder != "little":  # WAV 为小端序，大端机器需交换字节序
        samples.byteswap()

    expected = n_frames * channels
    if len(samples) != expected:
        raise MergeError(
            "WAV 数据长度异常: 实际样本数 {} 与帧信息推算值 {} 不一致".format(len(samples), expected)
        )
    return samples, channels, sample_rate, n_frames


def load_markers(path, total_frames, sample_rate):
    """解析 JSON 标记文件并校验，返回标记列表 [{start, end}] 与警告列表。

    校验规则：
      * 文件必须存在且为合法 JSON，顶层为列表；
      * 每个标记必须含数值型 start / end（秒）；
      * start < end，且 start 必须在音频时长内（含等于时长均非法）；
      * end 超出音频末尾时截断到末尾并产生警告。
    """
    if not os.path.isfile(path):
        raise MergeError("标记文件不存在: {}".format(path))
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except json.JSONDecodeError as exc:
        raise MergeError("标记文件 JSON 格式错误: {}；{}".format(path, exc))
    except OSError as exc:
        raise MergeError("标记文件无法读取: {}；{}".format(path, exc))

    if not isinstance(data, list):
        raise MergeError("标记文件 JSON 格式错误: 顶层结构必须是区间列表（JSON array）")

    duration = total_frames / sample_rate
    markers = []
    warnings = []
    for idx, item in enumerate(data):
        if not isinstance(item, dict):
            raise MergeError("标记 #{} 无效: 每个标记必须是含 start/end 的对象".format(idx))
        if "start" not in item or "end" not in item:
            raise MergeError("标记 #{} 无效: 必须包含 start 和 end 字段".format(idx))
        start, end = item["start"], item["end"]
        # bool 是 int 的子类，标记语义上不接受布尔值
        if isinstance(start, bool) or isinstance(end, bool) or not isinstance(start, (int, float)) \
                or not isinstance(end, (int, float)):
            raise MergeError("标记 #{} 无效: start/end 必须是数值（秒）".format(idx))
        if start < 0:
            raise MergeError("标记 #{} 无效: start 不能为负数（{}）".format(idx, start))
        if not start < end:
            raise MergeError(
                "标记 #{} 无效: 区间无效，要求 start < end，实际 start={}, end={}".format(
                    idx, start, end
                )
            )
        if start >= duration:
            raise MergeError(
                "标记 #{} 无效: start={}s 超出音频时长（{}s）".format(idx, start, duration)
            )

        start_frame = int(round(start * sample_rate))
        end_frame = int(round(end * sample_rate))
        if end_frame > total_frames:
            warnings.append(
                "标记 #{} 区间末尾 {}s 超出音频时长 {}s，已截断到音频末尾".format(
                    idx, end, duration
                )
            )
            end_frame = total_frames
        if end_frame <= start_frame:
            raise MergeError(
                "标记 #{} 无效: 对齐到整样本后区间为空（start={}s, end={}s）".format(idx, start, end)
            )
        markers.append({"start": start, "end": end,
                        "start_frame": start_frame, "end_frame": end_frame})
    return markers, warnings


def extract_segment(samples, channels, start_frame, end_frame):
    """按整样本帧边界提取一个片段，返回新的 array('h')。"""
    lo = start_frame * channels
    hi = end_frame * channels
    return samples[lo:hi]


def apply_crossfade(segments, channels, fade_frames):
    """在每个连接处做非重叠线性交叉淡化，保持总样本数不变。

    对相邻两段 A、B，取连接处各 fade_frames 帧：
      * A 尾部帧增益由 1 线性降到 0（淡出）；
      * B 头部帧增益由 0 线性升到 1（淡入）。
    两段不重叠，因此输出总样本数与无淡化时完全一致。
    每声道独立计算。
    """
    if fade_frames <= 0 or len(segments) < 2:
        return
    for i in range(len(segments) - 1):
        seg_a, seg_b = segments[i], segments[i + 1]
        n = min(fade_frames, len(seg_a) // channels, len(seg_b) // channels)
        if n == 0:
            continue
        # 前一段尾部淡出
        base = len(seg_a) - n * channels
        for k in range(n):
            gain = (n - 1 - k) / n  # 趋近 0
            for ch in range(channels):
                pos = base + k * channels + ch
                seg_a[pos] = int(round(seg_a[pos] * gain))
        # 后一段头部淡入
        for k in range(n):
            gain = k / n  # 从 0 起
            for ch in range(channels):
                pos = k * channels + ch
                seg_b[pos] = int(round(seg_b[pos] * gain))


def merge(samples, channels, sample_rate, total_frames, markers, fade_ms):
    """按标记顺序提取并合并片段，返回 (merged_samples, merged_count, warnings)。"""
    warnings = []
    if not markers:
        warnings.append("无片段输入: 标记列表为空，已生成 0.5 秒静音 WAV")
        silence_frames = int(round(0.5 * sample_rate))
        merged = array.array("h", [0]) * (silence_frames * channels)
        return merged, 0, warnings

    if fade_ms is not None and fade_ms < 0:
        raise MergeError("交叉淡化时长不能为负数（{} ms）".format(fade_ms))
    fade_frames = int(round((fade_ms or 0) / 1000.0 * sample_rate))

    segments = []
    merged_count = 0
    for idx, m in enumerate(markers):
        seg = extract_segment(samples, channels, m["start_frame"], m["end_frame"])
        if len(seg) == 0:
            warnings.append("标记 #{} 对齐后无有效样本，已跳过".format(idx))
            continue
        segments.append(seg)
        merged_count += 1

    apply_crossfade(segments, channels, fade_frames)

    merged = array.array("h")
    for seg in segments:
        merged.extend(seg)
    return merged, merged_count, warnings


def write_wav(path, samples, channels, sample_rate):
    """写出 16-bit PCM WAV，自动创建输出目录。"""
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    out = array.array("h", samples)
    if sys.byteorder != "little":
        out.byteswap()
    with wave.open(path, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(out.tobytes())


def run(wav_path, markers_path, output_path, report_path=None, fade_ms=None):
    """主流程：校验 -> 提取 -> 淡化 -> 写 WAV -> 写报告。返回报告字典。"""
    samples, channels, sample_rate, total_frames = load_wav(wav_path)
    markers, warnings = load_markers(markers_path, total_frames, sample_rate)
    merged, merged_count, merge_warnings = merge(
        samples, channels, sample_rate, total_frames, markers, fade_ms
    )
    warnings.extend(merge_warnings)

    write_wav(output_path, merged, channels, sample_rate)

    out_frames = len(merged) // channels
    report = {
        "output_path": os.path.abspath(output_path),
        "source_wav": os.path.abspath(wav_path),
        "sample_rate": sample_rate,
        "channels": channels,
        "merged_segments": int(merged_count),
        "total_frames": int(out_frames),
        "duration": out_frames / sample_rate,
        "crossfade_ms": float(fade_ms) if fade_ms else 0.0,
        "warnings": warnings,
    }
    if report_path is None:
        report_path = os.path.splitext(output_path)[0] + "_report.json"
    report_parent = os.path.dirname(os.path.abspath(report_path))
    os.makedirs(report_parent, exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    report["_report_path"] = os.path.abspath(report_path)
    return report


def build_parser():
    parser = argparse.ArgumentParser(
        description="按 JSON 标记区间从 16-bit PCM WAV 中提取片段并合并为新音轨（标准库实现）。"
    )
    parser.add_argument("wav", help="输入 WAV 文件路径（16-bit PCM，1 或 2 声道）")
    parser.add_argument("markers", help="标记 JSON 文件路径（区间列表，含 start/end，单位秒）")
    parser.add_argument("output", help="输出 WAV 文件路径")
    parser.add_argument("--report", dest="report", default=None,
                        help="JSON 报告输出路径（默认与输出 WAV 同名的 _report.json）")
    parser.add_argument("--crossfade", dest="crossfade", type=float, default=None,
                        help="连接处交叉淡化时长（毫秒），仅作用于片段连接处，默认不淡化")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        report = run(args.wav, args.markers, args.output, args.report, args.crossfade)
    except MergeError as exc:
        sys.stderr.write("错误: {}\n".format(exc))
        return 1
    except Exception as exc:  # 防御性兜底，保证任何失败都以非零码退出且信息明确
        sys.stderr.write("错误: 处理失败: {}\n".format(exc))
        return 1

    print("合并完成: {}".format(report["output_path"]))
    print("合并片段数: {}".format(report["merged_segments"]))
    print("总时长: {:.6f} 秒（{} 帧）".format(report["duration"], report["total_frames"]))
    print("报告: {}".format(report["_report_path"]))
    if report["warnings"]:
        print("警告 ({}):".format(len(report["warnings"])))
        for w in report["warnings"]:
            print("  - {}".format(w))
    return 0


if __name__ == "__main__":
    sys.exit(main())
