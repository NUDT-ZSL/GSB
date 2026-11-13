#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""播客片段预处理工具。

扫描输入目录中的 .wav 片段，仅处理单声道 / 44100Hz / 16bit PCM 文件：
  1. 检测连续静音区间（低于阈值且持续不少于最小时长），以毫秒起点+毫秒时长记录；
  2. 对片段首尾各 0.2 秒做线性音量平滑过渡（淡入/淡出），中段逐采样保持原样；
  3. 处理结果写入输出目录（沿用原文件名，同名覆盖并在报告中注明）；
  4. 运行结束生成 JSON 报告；存在跳过/失败片段时在 stderr 汇总提醒，退出码仍为 0。

仅使用 Python 标准库。
"""

import argparse
import json
import math
import os
import struct
import sys
import wave

__version__ = "1.0.0"

REQUIRED_RATE = 44100
REQUIRED_CHANNELS = 1
REQUIRED_SAMPWIDTH = 2  # 16bit
FADE_SECONDS = 0.2
INT16_MIN, INT16_MAX = -32768, 32767

STATUS_PROCESSED = "processed"
STATUS_SKIPPED = "skipped"
STATUS_FAILED = "failed"


def parse_args(argv):
    p = argparse.ArgumentParser(
        prog="podcast_prep",
        description="播客片段预处理：静音区间标记 + 首尾各0.2秒平滑过渡，"
                    "仅处理单声道/44100Hz/16bit PCM WAV，输出 JSON 报告。",
    )
    p.add_argument("input_dir", help="输入目录（扫描其中全部 .wav 文件）")
    p.add_argument("-o", "--output-dir", default="output",
                   help="输出目录（默认: output）")
    p.add_argument("--threshold-db", type=float, default=-40.0,
                   help="静音阈值，单位 dBFS（默认: -40）")
    p.add_argument("--min-silence", type=float, default=0.5,
                   help="静音最小时长，单位秒（默认: 0.5）")
    p.add_argument("--list", dest="list_file", default=None,
                   help="文本清单路径：每行一个文件名，空行与 # 开头行忽略；"
                        "仅处理清单内且存在于输入目录的文件")
    p.add_argument("--report", default=None,
                   help="JSON 报告路径（默认: <输出目录>/report.json）")
    p.add_argument("--version", action="version",
                   version="%(prog)s " + __version__)
    return p.parse_args(argv)


def read_list_file(path):
    """读取清单文件，返回文件名列表（忽略空行与 # 注释行）。失败抛 OSError。"""
    names = []
    with open(path, "r", encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            names.append(line)
    return names


def detect_silence(samples, threshold_amp, min_silence_samples, rate):
    """检测连续静音区间，返回 [{start_ms, duration_ms}, ...]。

    区间从片段开头起算；起点为 0 或结束正好落在片段末尾的区间同样记录。
    """
    intervals = []
    n = len(samples)
    i = 0
    while i < n:
        if abs(samples[i]) <= threshold_amp:
            j = i + 1
            while j < n and abs(samples[j]) <= threshold_amp:
                j += 1
            if j - i >= min_silence_samples:
                start_ms = int(round(i * 1000.0 / rate))
                end_ms = int(round(j * 1000.0 / rate))
                intervals.append({"start_ms": start_ms,
                                  "duration_ms": end_ms - start_ms})
            i = j
        else:
            i += 1
    return intervals


def apply_fades(samples, fade_len):
    """首尾各 fade_len 个采样做线性淡入/淡出，中段逐采样保持原样。"""
    n = len(samples)
    out = list(samples)
    if n < 2:
        return out
    f = min(fade_len, n // 2)
    for i in range(f):
        g = i / f  # 0 -> (f-1)/f，边界处增益为 0，向中段单调上升
        v_in = int(round(samples[i] * g))
        v_out = int(round(samples[n - 1 - i] * g))
        out[i] = max(INT16_MIN, min(INT16_MAX, v_in))
        out[n - 1 - i] = max(INT16_MIN, min(INT16_MAX, v_out))
    return out


def process_file(name, in_path, out_path, threshold_amp, min_silence_samples):
    """处理单个文件，返回报告条目。"""
    entry = {"name": name, "status": None, "silence_intervals": None,
             "skip_reason": None, "overwritten": None}

    if not os.path.isfile(in_path):
        entry["status"] = STATUS_SKIPPED
        entry["skip_reason"] = "文件不存在于输入目录"
        return entry

    try:
        wf = wave.open(in_path, "rb")
    except (wave.Error, EOFError, OSError) as e:
        entry["status"] = STATUS_FAILED
        entry["skip_reason"] = "文件损坏或无法读取: %s" % e
        return entry

    with wf:
        channels = wf.getnchannels()
        rate = wf.getframerate()
        width = wf.getsampwidth()
        comptype = wf.getcomptype()

        if comptype != "NONE":
            entry["status"] = STATUS_SKIPPED
            entry["skip_reason"] = "非PCM压缩格式(%s)" % comptype
            return entry
        if channels != REQUIRED_CHANNELS:
            entry["status"] = STATUS_SKIPPED
            entry["skip_reason"] = "声道数为%d，需单声道" % channels
            return entry
        if rate != REQUIRED_RATE:
            entry["status"] = STATUS_SKIPPED
            entry["skip_reason"] = "采样率%dHz，需44100Hz" % rate
            return entry
        if width != REQUIRED_SAMPWIDTH:
            entry["status"] = STATUS_SKIPPED
            entry["skip_reason"] = "采样位深%dbit，需16bit" % (width * 8)
            return entry

        try:
            nframes = wf.getnframes()
            raw = wf.readframes(nframes)
        except (wave.Error, EOFError, OSError) as e:
            entry["status"] = STATUS_FAILED
            entry["skip_reason"] = "读取音频数据失败: %s" % e
            return entry

    if len(raw) < nframes * width:
        entry["status"] = STATUS_FAILED
        entry["skip_reason"] = "音频数据不完整（实际%d字节，预期%d字节）" % (
            len(raw), nframes * width)
        return entry

    samples = struct.unpack("<%dh" % nframes, raw)

    intervals = detect_silence(samples, threshold_amp, min_silence_samples, rate)
    faded = apply_fades(samples, int(FADE_SECONDS * rate))
    out_raw = struct.pack("<%dh" % len(faded), *faded)

    overwritten = os.path.exists(out_path)
    with wave.open(out_path, "wb") as wo:
        wo.setnchannels(REQUIRED_CHANNELS)
        wo.setsampwidth(REQUIRED_SAMPWIDTH)
        wo.setframerate(rate)
        wo.writeframes(out_raw)

    entry["status"] = STATUS_PROCESSED
    entry["silence_intervals"] = intervals
    entry["overwritten"] = overwritten
    return entry


def main(argv=None):
    args = parse_args(argv)

    if not os.path.isdir(args.input_dir):
        print("错误: 输入目录不存在或不是目录: %s" % args.input_dir,
              file=sys.stderr)
        return 2

    if args.list_file is not None:
        try:
            names = read_list_file(args.list_file)
        except OSError as e:
            print("错误: 清单文件不可读: %s (%s)" % (args.list_file, e),
                  file=sys.stderr)
            return 2
    else:
        names = sorted(f for f in os.listdir(args.input_dir)
                       if f.lower().endswith(".wav")
                       and os.path.isfile(os.path.join(args.input_dir, f)))

    os.makedirs(args.output_dir, exist_ok=True)
    report_path = args.report or os.path.join(args.output_dir, "report.json")

    threshold_amp = 32768.0 * (10.0 ** (args.threshold_db / 20.0))
    min_silence_samples = int(math.ceil(args.min_silence * REQUIRED_RATE))

    entries = []
    for name in names:
        in_path = os.path.join(args.input_dir, name)
        out_path = os.path.join(args.output_dir, name)
        entries.append(process_file(name, in_path, out_path,
                                    threshold_amp, min_silence_samples))

    counts = {STATUS_PROCESSED: 0, STATUS_SKIPPED: 0, STATUS_FAILED: 0}
    for e in entries:
        counts[e["status"]] += 1

    report = {
        "total": len(entries),
        "processed": counts[STATUS_PROCESSED],
        "skipped": counts[STATUS_SKIPPED],
        "failed": counts[STATUS_FAILED],
        "parameters": {
            "threshold_db": args.threshold_db,
            "min_silence_seconds": args.min_silence,
            "fade_seconds": FADE_SECONDS,
        },
        "files": entries,
    }
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    problems = [e for e in entries if e["status"] != STATUS_PROCESSED]
    if problems:
        print("提醒: 以下 %d 个片段被跳过或不可用（本次运行退出码仍为0，"
              "修正后可重跑）:" % len(problems), file=sys.stderr)
        for e in problems:
            print("  - %s [%s]: %s" % (e["name"], e["status"],
                                       e["skip_reason"]), file=sys.stderr)

    print("完成: 共%d个，处理%d，跳过%d，失败%d。报告: %s"
          % (report["total"], report["processed"], report["skipped"],
             report["failed"], report_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
