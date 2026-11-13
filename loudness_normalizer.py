#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
loudness_normalizer.py
======================

播客语音响度归一化工具（仅使用 Python 标准库）。

功能
----
读取 16 位 / 44.1kHz / 单声道 PCM WAV 长对话录音：

1. 按短时能量划分语音段与静音段（10ms 无重叠分析帧）；
2. 计算每个语音段的 RMS（以满量程 32768 归一化），取所有语音段
   RMS 的中位数作为目标响度 target_rms；
3. 对时长 >= 0.1 秒的语音段施加增益，使其 RMS 接近目标响度，
   实际增益截断在 [0.5, 2.0]；静音段与不足 0.1 秒的短语音段保持原样，
   报告中增益记为 1.0；
4. 增益后的采样值截断到 16 位有符号整数范围 [-32768, 32767]；
5. 输出与输入同格式（16 位 / 单声道 / 44.1kHz）、帧数完全一致的 WAV，
   以及 JSON 报告。

边界处理
--------
* 格式不符（采样率不是 44100Hz、非单声道、非 16 位 PCM、无法解析、
  文件不存在）：不生成输出 WAV，写出含 "error" 字段的报告，退出码 3；
* 检测不到语音段：输出 WAV 与输入逐字节相同，报告 message 为
  'no speech detected'，退出码 0；
* 输出目录不可写导致写入失败：进程不崩溃，报告（尽可能写入回退位置）
  记录 error，退出码 3；
* 输出目录不存在时自动创建。

命令行
------
    python loudness_normalizer.py <input.wav> <output_dir>

退出码：0 成功；2 参数错误；3 处理/写入错误（报告含 error 字段）。
"""

import argparse
import array
import json
import math
import os
import statistics
import sys
import time
import wave

# ----------------------------------------------------------------------
# 常量
# ----------------------------------------------------------------------
REQUIRED_RATE = 44100          # 要求的采样率 Hz
REQUIRED_WIDTH = 2             # 16 位 PCM = 2 字节
REQUIRED_CHANNELS = 1          # 单声道
FRAME_MS = 10                  # 分析帧长 10ms
HOP_MS = 10                    # 无重叠（帧移 = 帧长）
SILENCE_RMS_THRESHOLD = 0.01   # 帧能量阈值（满量程归一化 RMS）
MIN_SPEECH_SECONDS = 0.1       # 短于此长度的语音段不调整
MIN_GAIN = 0.5                 # 增益下限
MAX_GAIN = 2.0                 # 增益上限
INT16_MIN = -(2 ** 15)         # -32768
INT16_MAX = 2 ** 15 - 1        #  32767
FULL_SCALE = 2 ** 15           # 32768，RMS 归一化分母
TIME_DECIMALS = 3              # 报告时间保留 3 位小数
RMS_DECIMALS = 6               # RMS / 增益保留 6 位小数（保证 0.15 等精确值）


class NormalizeError(Exception):
    """业务级错误：携带给用户的错误信息。"""


# ----------------------------------------------------------------------
# WAV 读取与校验
# ----------------------------------------------------------------------
def read_wav(path):
    """读取 WAV，返回 (params, samples)。

    params 为 wave._wave_params；samples 为 array('h')。
    文件不存在 / 无法解析 / 格式不符时抛 NormalizeError。
    """
    if not os.path.exists(path):
        raise NormalizeError("input file does not exist: %s" % path)
    if not os.path.isfile(path):
        raise NormalizeError("input path is not a file: %s" % path)

    try:
        with wave.open(path, "rb") as wf:
            params = wf.getparams()
            raw = wf.readframes(params.nframes)
    except (wave.Error, EOFError, OSError) as exc:
        raise NormalizeError("cannot parse WAV file: %s (%s)" % (path, exc))

    if params.comptype != "NONE":
        raise NormalizeError(
            "only uncompressed PCM WAV is supported, got compression: %s"
            % params.comptype)
    if params.nchannels != REQUIRED_CHANNELS:
        raise NormalizeError(
            "mono WAV required, got %d channels" % params.nchannels)
    if params.sampwidth != REQUIRED_WIDTH:
        raise NormalizeError(
            "16-bit PCM WAV required, got sample width %d bytes"
            % params.sampwidth)
    if params.framerate != REQUIRED_RATE:
        raise NormalizeError(
            "sample rate must be %d Hz, got %d Hz"
            % (REQUIRED_RATE, params.framerate))

    try:
        samples = array.array("h")
        samples.frombytes(raw)
    except (ValueError, EOFError) as exc:
        raise NormalizeError("corrupt PCM data: %s" % exc)

    # 字节数必须与声明的帧数吻合（单声道 16 位，每帧 2 字节）
    if len(samples) != params.nframes:
        raise NormalizeError(
            "declared %d frames but %d samples decoded"
            % (params.nframes, len(samples)))
    return params, samples


# ----------------------------------------------------------------------
# 能量 / RMS 计算
# ----------------------------------------------------------------------
def rms_of(values):
    """满量程归一化 RMS；空序列返回 0.0。"""
    n = len(values)
    if n == 0:
        return 0.0
    total = 0
    for v in values:
        total += v * v
    return math.sqrt(total / n) / FULL_SCALE


def classify_frames(samples, rate):
    """按帧能量给每个采样帧打标签，返回与帧对齐的 (is_speech, frame_rms) 列表。

    帧长 FRAME_MS、帧移 HOP_MS，最后一帧不足帧长时按实际长度计算。
    """
    frame_len = int(rate * FRAME_MS / 1000)   # 882
    hop_len = int(rate * HOP_MS / 1000)       # 441
    labels = []
    total = len(samples)
    start = 0
    while start < total:
        end = min(start + frame_len, total)
        frame_rms = rms_of(samples[start:end])
        labels.append((frame_rms > SILENCE_RMS_THRESHOLD, frame_rms))
        if end >= total:
            break
        start += hop_len
    return labels, frame_len, hop_len


def build_segments(samples, rate):
    """合并相邻同标签帧为片段，返回片段 dict 列表（按 start 升序）。

    每个片段含：start, end, is_speech, original_rms。
    """
    labels, frame_len, hop_len = classify_frames(samples, rate)
    total = len(samples)
    segments = []
    if not labels:
        return segments

    cur_speech = labels[0][0]
    cur_start_sample = 0
    frame_idx = 1
    while frame_idx < len(labels):
        is_speech = labels[frame_idx][0]
        if is_speech != cur_speech:
            # 边界采样位置 = 当前帧起点，同时也是上一段终点
            boundary = min(frame_idx * hop_len, total)
            segments.append(_make_segment(
                samples, rate, cur_start_sample, boundary, cur_speech))
            cur_start_sample = boundary
            cur_speech = is_speech
        frame_idx += 1

    segments.append(_make_segment(
        samples, rate, cur_start_sample, total, cur_speech))
    return segments


def _make_segment(samples, rate, start_sample, end_sample, is_speech):
    seg_values = samples[start_sample:end_sample]
    return {
        "start": round(start_sample / rate, TIME_DECIMALS),
        "end": round(end_sample / rate, TIME_DECIMALS),
        "is_speech": is_speech,
        "original_rms": round(rms_of(seg_values), RMS_DECIMALS),
        "_start_sample": start_sample,
        "_end_sample": end_sample,
    }


def sort_segments_by_start(segments):
    """按 start 升序稳定排序（start 相同保持原有先后，Python sorted 为稳定排序）。"""
    return sorted(segments, key=lambda seg: seg["start"])


# ----------------------------------------------------------------------
# 增益计算与应用
# ----------------------------------------------------------------------
def clamp(value, low, high):
    return max(low, min(high, value))


def clamp_int16(value):
    """四舍五入并截断到 16 位有符号整数边界。"""
    rounded = int(math.floor(value + 0.5)) if value >= 0 else int(math.ceil(value - 0.5))
    return clamp(rounded, INT16_MIN, INT16_MAX)


def plan_gains(segments):
    """确定目标响度与每段增益。

    目标 = 所有语音段（含过短不调整的段）RMS 的中位数；无语音段时为 None。
    返回 (target_rms, adjusted_count, average_gain)，并就地写入每段
    target_rms / gain / adjusted 字段。
    """
    speech_rms = [seg["original_rms"] for seg in segments if seg["is_speech"]]
    target = statistics.median(speech_rms) if speech_rms else None

    adjusted_gains = []
    for seg in segments:
        seg["target_rms"] = round(target, RMS_DECIMALS) if target is not None else None
        eligible = (seg["is_speech"]
                    and (seg["end"] - seg["start"]) >= MIN_SPEECH_SECONDS - 1e-9)
        if eligible and target is not None and seg["original_rms"] > 0:
            ideal = target / seg["original_rms"]
            gain = clamp(ideal, MIN_GAIN, MAX_GAIN)
            seg["gain"] = round(gain, RMS_DECIMALS)
            seg["adjusted"] = True
            adjusted_gains.append(gain)
        else:
            # 静音段、过短语音段：不调整
            seg["gain"] = 1.0
            seg["adjusted"] = False

    adjusted_count = len(adjusted_gains)
    average_gain = (round(sum(adjusted_gains) / adjusted_count, RMS_DECIMALS)
                    if adjusted_count else None)
    return target, adjusted_count, average_gain


def apply_gains(samples, segments):
    """按每段增益生成输出采样数组（array('h')），溢出截断到 int16 边界。"""
    output = array.array("h", samples)  # 逐采样拷贝，静音段天然保持原样
    for seg in segments:
        if not seg["adjusted"]:
            continue
        gain = seg["gain"]
        for i in range(seg["_start_sample"], seg["_end_sample"]):
            output[i] = clamp_int16(samples[i] * gain)
    return output


# ----------------------------------------------------------------------
# 输出
# ----------------------------------------------------------------------
def write_wav(path, params, samples):
    with wave.open(path, "wb") as wf:
        wf.setnchannels(params.nchannels)
        wf.setsampwidth(params.sampwidth)
        wf.setframerate(params.framerate)
        wf.writeframes(samples.tobytes())


def _public_segment(seg):
    """剥离内部字段，生成报告中的片段条目。"""
    return {
        "start": seg["start"],
        "end": seg["end"],
        "type": "speech" if seg["is_speech"] else "silence",
        "original_rms": seg["original_rms"],
        "target_rms": seg["target_rms"],
        "gain": seg["gain"],
        "adjusted": seg["adjusted"],
    }


def build_report(segments, target, adjusted_count, average_gain,
                 elapsed_time, message=None):
    report = {
        "message": message if message is not None else "ok",
        "target_rms": round(target, RMS_DECIMALS) if target is not None else None,
        "adjusted_segments": adjusted_count,
        "average_gain": average_gain,
        "elapsed_time": round(elapsed_time, TIME_DECIMALS),
        "segments": [_public_segment(seg) for seg in segments],
    }
    return report


def write_report(path, report):
    with open(path, "w", encoding="utf-8") as fp:
        json.dump(report, fp, ensure_ascii=False, indent=2)


def fallback_report_path(input_path, output_dir):
    """输出目录不可写时的报告回退位置：输入文件同目录，再退到系统临时目录。"""
    base = os.path.splitext(os.path.basename(input_path))[0]
    candidates = [
        os.path.dirname(os.path.abspath(input_path)),
        os.environ.get("TEMP") or os.environ.get("TMP") or os.getcwd(),
    ]
    for directory in candidates:
        try:
            candidate = os.path.join(directory, base + "_report.json")
            # 试探可写
            with open(candidate, "a", encoding="utf-8"):
                pass
            return candidate
        except OSError:
            continue
    return None


# ----------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------
def process(input_path, output_dir):
    """完整处理流程，返回 report dict；失败抛 NormalizeError。"""
    started = time.perf_counter()
    params, samples = read_wav(input_path)  # 失败时无任何输出

    base = os.path.splitext(os.path.basename(input_path))[0]
    try:
        os.makedirs(output_dir, exist_ok=True)
    except OSError as exc:
        raise NormalizeError(
            "cannot create output directory '%s': %s" % (output_dir, exc))

    wav_out = os.path.join(output_dir, base + "_normalized.wav")
    report_out = os.path.join(output_dir, base + "_report.json")

    segments = build_segments(samples, params.framerate)
    segments = sort_segments_by_start(segments)
    target, adjusted_count, average_gain = plan_gains(segments)

    speech_exists = any(seg["is_speech"] for seg in segments)

    try:
        if not speech_exists:
            # 无语音：输出与输入逐字节相同（直接复制文件字节）
            with open(input_path, "rb") as src, open(wav_out, "wb") as dst:
                dst.write(src.read())
            message = "no speech detected"
        else:
            output_samples = apply_gains(samples, segments)
            write_wav(wav_out, params, output_samples)
            message = "ok"

        elapsed = time.perf_counter() - started
        report = build_report(segments, target, adjusted_count,
                              average_gain, elapsed, message)
        write_report(report_out, report)
    except OSError as exc:
        raise NormalizeError("failed to write outputs: %s" % exc)

    return report, wav_out, report_out


def fail(input_path, output_dir, message):
    """错误出口：尽量把含 error 的报告写到输出目录，失败则写回退位置。"""
    payload = {"error": message}
    written = None
    try:
        os.makedirs(output_dir, exist_ok=True)
        base = os.path.splitext(os.path.basename(input_path))[0] or "input"
        written = os.path.join(output_dir, base + "_report.json")
        write_report(written, payload)
    except OSError:
        written = fallback_report_path(input_path, output_dir)
        if written:
            try:
                write_report(written, payload)
            except OSError:
                written = None
    return written


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Normalize speech loudness of a mono 16-bit 44.1kHz PCM WAV.")
    parser.add_argument("input_wav", help="path to the input WAV file")
    parser.add_argument("output_dir", help="directory for output WAV and JSON report")
    args = parser.parse_args(argv)

    try:
        report, wav_out, report_out = process(args.input_wav, args.output_dir)
    except NormalizeError as exc:
        written = fail(args.input_wav, args.output_dir, str(exc))
        print("ERROR: %s" % exc, file=sys.stderr)
        if written:
            print("error report written to: %s" % written, file=sys.stderr)
        return 3

    print("message: %s" % report["message"])
    print("target_rms: %s" % report["target_rms"])
    print("adjusted_segments: %d" % report["adjusted_segments"])
    print("average_gain: %s" % report["average_gain"])
    print("elapsed_time: %s s" % report["elapsed_time"])
    print("output WAV: %s" % wav_out)
    print("report: %s" % report_out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
