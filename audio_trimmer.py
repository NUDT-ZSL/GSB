#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audio_trimmer.py — WAV 静音段批量去除工具（仅标准库，Windows 本地运行）

功能：
  扫描输入目录下全部 .wav 文件（含中文文件名），按文件名升序逐个处理：
  去除非静音片段之间的静音段（以及首尾静音），拼接为连续有效音频，
  统一输出 16 位 PCM 单声道（采样率保持与源一致）到独立输出目录，
  并生成 JSON 清单与控制台摘要。

用法：
  python audio_trimmer.py <输入目录> <输出目录> [--threshold -45] [--min-silence 0.3] [--manifest 路径]

参数校验：
  --threshold    静音阈值（dBFS），合法范围 (-90, -20]，默认 -45
  --min-silence  最短静音时长（秒），必须 > 0，默认 0.3
  非法取值打印提示并以非零退出码退出。
"""
import argparse
import json
import math
import struct
import sys
import wave
from pathlib import Path

STATUS_OK = "成功"
STATUS_FAIL = "失败"
STATUS_EMPTY = "无有效音频"

DEFAULT_THRESHOLD_DB = -45.0
DEFAULT_MIN_SILENCE = 0.3

# 阈值合法范围：(-90, -20] dBFS。-90 及以下无实际意义，-20 以上会把正常内容误判为静音。
THRESHOLD_LOW_EXCLUSIVE = -90.0
THRESHOLD_HIGH_INCLUSIVE = -20.0

PCM16_MAX = 32767


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="audio_trimmer",
        description="批量去除 WAV 文件静音段，输出 16 位 PCM 单声道并生成 JSON 清单。",
    )
    parser.add_argument("input_dir", help="输入目录（扫描其中全部 .wav 文件）")
    parser.add_argument("output_dir", help="输出目录（不存在则自动创建）")
    parser.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_THRESHOLD_DB,
        help="静音判定阈值（dBFS），合法范围 (-90, -20]，默认 %(default)s",
    )
    parser.add_argument(
        "--min-silence",
        type=float,
        default=DEFAULT_MIN_SILENCE,
        help="最短静音时长（秒），必须 > 0，默认 %(default)s",
    )
    parser.add_argument(
        "--manifest",
        default=None,
        help="清单 JSON 输出路径，默认 <输出目录>/manifest.json",
    )
    args = parser.parse_args(argv)

    if not (THRESHOLD_LOW_EXCLUSIVE < args.threshold <= THRESHOLD_HIGH_INCLUSIVE):
        parser.error(
            f"非法静音阈值 {args.threshold} dBFS：须在 ({THRESHOLD_LOW_EXCLUSIVE}, "
            f"{THRESHOLD_HIGH_INCLUSIVE}] 之间，例如 -45"
        )
    if args.min_silence <= 0:
        parser.error(f"非法最短静音时长 {args.min_silence} 秒：必须大于 0，例如 0.3")
    return args


# ---------------------------------------------------------------- WAV 读取

def decode_frames_to_int16(raw, sampwidth):
    """把 PCM 原始字节解码为 16 位量级的整数样本列表。"""
    if sampwidth == 1:  # 8 位无符号
        return [(b - 128) << 8 for b in raw]
    if sampwidth == 2:  # 16 位有符号
        count = len(raw) // 2
        return list(struct.unpack("<%dh" % count, raw[: count * 2]))
    if sampwidth == 3:  # 24 位有符号
        out = []
        for i in range(0, len(raw) - 2, 3):
            v = raw[i] | (raw[i + 1] << 8) | (raw[i + 2] << 16)
            if v & 0x800000:
                v -= 1 << 24
            out.append(v >> 8)
        return out
    if sampwidth == 4:  # 32 位有符号
        count = len(raw) // 4
        return [v >> 16 for v in struct.unpack("<%di" % count, raw[: count * 4])]
    raise ValueError(f"不支持的样本位深: {sampwidth * 8} 位")


def mix_to_mono(samples, channels):
    """多声道按帧取平均混为单声道。"""
    if channels == 1:
        return samples
    mono = []
    for i in range(0, len(samples) - channels + 1, channels):
        mono.append(sum(samples[i : i + channels]) // channels)
    return mono


def read_wav_as_mono16(path):
    """读取 WAV，返回 (单声道16位样本列表, 采样率)。读取失败抛异常。"""
    with wave.open(str(path), "rb") as w:
        channels = w.getnchannels()
        sampwidth = w.getsampwidth()
        rate = w.getframerate()
        frames = w.getnframes()
        raw = w.readframes(frames)
    samples = decode_frames_to_int16(raw, sampwidth)
    return mix_to_mono(samples, channels), rate


# ---------------------------------------------------------------- 静音去除

def remove_silence(mono, rate, threshold_db, min_silence):
    """
    去除静音段，返回拼接后的样本列表；整段无有效音频时返回 None。

    判定：|样本| >= 阈值幅值 为有效样本。
    静音段长度 >= min_silence 才切除；更短的静音（如过零点）保留，
    保证输出首尾样本与源非静音段精确一致。
    """
    threshold_amp = PCM16_MAX * (10.0 ** (threshold_db / 20.0))
    min_run = max(1, int(round(min_silence * rate)))

    loud_idx = [i for i, s in enumerate(mono) if abs(s) >= threshold_amp]
    if not loud_idx:
        return None

    # 以有效样本为锚点划分片段：相邻有效样本间隔 < min_run 时合并（保留短静音）
    segments = [[loud_idx[0], loud_idx[0]]]
    for i in loud_idx[1:]:
        if i - segments[-1][1] - 1 < min_run:
            segments[-1][1] = i
        else:
            segments.append([i, i])

    out = []
    for start, end in segments:
        out.extend(mono[start : end + 1])
    return out


# ---------------------------------------------------------------- WAV 写入

def write_wav_mono16(path, mono, rate):
    data = struct.pack(
        "<%dh" % len(mono),
        *[max(-32768, min(32767, int(s))) for s in mono],
    )
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(data)


def unique_output_path(output_dir, stem):
    """原名加 _processed 后缀；冲突时追加序号，绝不覆盖已有文件。"""
    candidate = output_dir / f"{stem}_processed.wav"
    serial = 1
    while candidate.exists():
        candidate = output_dir / f"{stem}_processed_{serial}.wav"
        serial += 1
    return candidate


# ---------------------------------------------------------------- 批处理

def process_one(src_path, output_dir, threshold_db, min_silence):
    """处理单个文件，返回清单条目。"""
    entry = {
        "source": str(src_path),
        "output": None,
        "status": STATUS_FAIL,
        "error": None,
    }
    try:
        mono, rate = read_wav_as_mono16(src_path)
        trimmed = remove_silence(mono, rate, threshold_db, min_silence)
        if trimmed is None:
            entry["status"] = STATUS_EMPTY
            return entry
        out_path = unique_output_path(output_dir, src_path.stem)
        write_wav_mono16(out_path, trimmed, rate)
        entry["output"] = str(out_path)
        entry["status"] = STATUS_OK
    except Exception as exc:  # 坏文件不中断批处理
        entry["status"] = STATUS_FAIL
        entry["error"] = f"{type(exc).__name__}: {exc}"
    return entry


def main(argv=None):
    args = parse_args(argv)
    input_dir = Path(args.input_dir)
    output_dir = Path(args.output_dir)
    if not input_dir.is_dir():
        print(f"错误：输入目录不存在: {input_dir}", file=sys.stderr)
        return 2
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = Path(args.manifest) if args.manifest else output_dir / "manifest.json"

    wav_files = sorted(
        (p for p in input_dir.iterdir() if p.is_file() and p.suffix.lower() == ".wav"),
        key=lambda p: p.name,
    )

    entries = [
        process_one(p, output_dir, args.threshold, args.min_silence) for p in wav_files
    ]

    manifest_path.write_text(
        json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    ok = [e for e in entries if e["status"] == STATUS_OK]
    fail = [e for e in entries if e["status"] == STATUS_FAIL]
    empty = [e for e in entries if e["status"] == STATUS_EMPTY]

    print("===== 处理摘要 =====")
    print(f"处理总数: {len(entries)}")
    print(f"成功: {len(ok)}")
    print(f"失败: {len(fail)}")
    print(f"无有效音频: {len(empty)}")
    if fail:
        print("失败文件:")
        for e in sorted(fail, key=lambda e: Path(e["source"]).name):
            print(f"  - {Path(e['source']).name}: {e['error']}")
    print(f"清单已保存: {manifest_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
