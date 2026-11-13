#!/usr/bin/env python3
"""wav_trim — 播客片头片尾修剪 + 音量包络命令行工具。

输入: 16 位 PCM 双声道 WAV;可选剪切区间(秒)与音量包络列表。
输出: 保持原采样率/声道数/位深的 WAV,并在输出同目录生成 report.json。

用法:
    python wav_trim.py input.wav output.wav [--trim START END]
                        [--envelope START,END,GAIN] ...

包络语义:
  - 每个包络为 (start, end, gain) 三元组,时间单位为秒,基于输入(原始)时间轴;
  - 区间 [start, end) 内应用恒定增益,区间外不变;
  - 包络可重叠,重叠处增益相乘;
  - 增益钳位到 [0.0, 2.0];
  - 每个样本乘以增益后饱和截断到 int16 [-32768, 32767],不回绕。

退出码: 0 成功;1 处理错误(格式不支持/剪切区间非法/静音输入);2 命令行用法错误。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import wave
from array import array

CHUNK_FRAMES = 262144          # 每次读写的帧数,限制内存占用
GAIN_MIN, GAIN_MAX = 0.0, 2.0  # 增益钳位范围
INT16_MIN, INT16_MAX = -32768, 32767
_EPS = 1e-9                    # 时间->帧换算的浮点保护


class ProcessingError(Exception):
    """可预期的处理错误(退出码 1)。"""


# ---------------------------------------------------------------- 参数解析

def parse_envelope(text: str):
    """解析 'start,end,gain' 三元组;非法时抛 ValueError(由 parser.error 转为退出码 2)。"""
    parts = [p.strip() for p in text.split(",")]
    if len(parts) != 3:
        raise ValueError("包络须为 start,end,gain 三个数值")
    try:
        start, end, gain = (float(p) for p in parts)
    except ValueError:
        raise ValueError(f"包络的 start/end/gain 必须为数字: {text!r}") from None
    if not all(math.isfinite(v) for v in (start, end, gain)):
        raise ValueError("包络数值必须为有限数字")
    return (start, end, gain)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="wav_trim",
        description="修剪 16 位 PCM 双声道 WAV 并应用音量包络,输出 WAV 与 report.json。",
    )
    p.add_argument("input", help="输入 WAV(16 位 PCM 双声道)")
    p.add_argument("output", help="输出 WAV 路径")
    p.add_argument("--trim", nargs=2, type=float, metavar=("START", "END"),
                   default=None, help="剪切起止秒,超出时长按边界限制")
    p.add_argument("--envelope", action="append", default=[],
                   metavar="START,END,GAIN",
                   help="音量包络三元组,可重复;重叠处增益相乘,增益钳位到 [0.0, 2.0]")
    return p


# ---------------------------------------------------------------- 音频处理

def _time_to_frame(t: float, framerate: int) -> int:
    """时间 t(秒)-> 满足 t <= frame/framerate 的最小帧号(区间 [start, end) 语义)。"""
    return int(math.ceil(t * framerate - _EPS))


def _clamp16(v: int) -> int:
    if v > INT16_MAX:
        return INT16_MAX
    if v < INT16_MIN:
        return INT16_MIN
    return v


def _apply_gain(data: bytes, gain: float) -> bytes:
    """对一块 16 位小端 PCM 数据逐样本乘增益并饱和截断。"""
    src = array("h")
    src.frombytes(data)
    if sys.byteorder == "big":
        src.byteswap()
    out = array("h", [_clamp16(int(round(s * gain))) for s in src])
    if sys.byteorder == "big":
        out.byteswap()
    return out.tobytes()


def _build_segments(envelopes, start_frame: int, end_frame: int, framerate: int):
    """把 [start_frame, end_frame) 切分为恒定增益的连续段(重叠包络增益相乘)。"""
    bounds = {start_frame, end_frame}
    spans = []
    for s, e, g in envelopes:
        fs = max(_time_to_frame(s, framerate), start_frame)
        fe = min(_time_to_frame(e, framerate), end_frame)
        if fs < fe:
            spans.append((fs, fe, g))
            bounds.add(fs)
            bounds.add(fe)
    ordered = sorted(bounds)
    segments = []
    for a, b in zip(ordered, ordered[1:]):
        gain = 1.0
        for fs, fe, g in spans:
            if fs <= a and b <= fe:
                gain *= g
        segments.append((a, b, gain))
    return segments


def process(input_path: str, output_path: str, trim, envelopes) -> dict:
    """执行处理,返回报告数据;失败抛 ProcessingError(不产生输出文件)。"""
    with wave.open(input_path, "rb") as w:
        comptype = w.getcomptype()
        sampwidth = w.getsampwidth()
        nchannels = w.getnchannels()
        if comptype != "NONE" or sampwidth != 2 or nchannels != 2:
            raise ProcessingError(
                "unsupported format: comptype=%r, %d-bit, %d channel(s); "
                "only 16-bit PCM stereo WAV is supported"
                % (comptype, sampwidth * 8, nchannels))
        framerate = w.getframerate()
        nframes = w.getnframes()
        duration = nframes / framerate

        # ---- 剪切区间:越界按边界限制,倒序报错 ----
        if trim is None:
            start_s, end_s = 0.0, duration
        else:
            start_s = min(max(trim[0], 0.0), duration)
            end_s = min(max(trim[1], 0.0), duration)
        if start_s >= end_s:
            raise ProcessingError(
                "trim start (%.6fs) >= end (%.6fs) after clamping to [0, %.6fs]"
                % (start_s, end_s, duration))
        start_frame = int(round(start_s * framerate))
        end_frame = int(round(end_s * framerate))
        if start_frame >= end_frame:
            raise ProcessingError("trim range selects zero frames")

        segments = _build_segments(envelopes, start_frame, end_frame, framerate)

        # ---- 第一遍:静音检查(选中区域内是否全零) ----
        w.setpos(start_frame)
        remaining = end_frame - start_frame
        silent = True
        while remaining > 0:
            data = w.readframes(min(CHUNK_FRAMES, remaining))
            if not data:
                break
            if data.strip(b"\x00"):
                silent = False
                break
            remaining -= len(data) // 4
        if silent:
            raise ProcessingError("输入音频为静音 (input audio is all silence)")

        # ---- 第二遍:分段写出 ----
        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        w.setpos(start_frame)
        with wave.open(output_path, "wb") as out:
            out.setnchannels(2)
            out.setsampwidth(2)
            out.setframerate(framerate)
            for seg_start, seg_end, gain in segments:
                w.setpos(seg_start)
                left = seg_end - seg_start
                while left > 0:
                    data = w.readframes(min(CHUNK_FRAMES, left))
                    if not data:
                        break
                    left -= len(data) // 4
                    if gain == 1.0:
                        out.writeframes(data)   # 未覆盖区原样写出
                    else:
                        out.writeframes(_apply_gain(data, gain))

    return {
        "input_path": os.path.abspath(input_path),
        "output_path": os.path.abspath(output_path),
        "sample_rate": framerate,
        "channels": 2,
        "bit_depth": 16,
        "trim": {"start": start_s, "end": end_s},
        "duration": (end_frame - start_frame) / framerate,
        "frames": end_frame - start_frame,
        "envelopes": [{"start": s, "end": e, "gain": g} for s, e, g in envelopes],
    }


# ---------------------------------------------------------------- 入口

def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    envelopes = []
    for raw in args.envelope:
        try:
            s, e, g = parse_envelope(raw)
        except ValueError as exc:
            parser.error("invalid --envelope %s: %s" % (raw, exc))  # 退出码 2 + 用法
        envelopes.append((s, e, min(GAIN_MAX, max(GAIN_MIN, g))))   # 增益钳位

    try:
        report = process(args.input, args.output, args.trim, envelopes)
    except ProcessingError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    except (OSError, wave.Error, EOFError) as exc:
        print("error: cannot process input: %s" % exc, file=sys.stderr)
        return 1

    report_path = os.path.join(
        os.path.dirname(os.path.abspath(args.output)), "report.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print("OK: %s (%.3fs) -> report: %s"
          % (report["output_path"], report["duration"], report_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
