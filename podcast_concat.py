#!/usr/bin/env python3
"""podcast_concat.py — 播客精彩片段合集拼接工具(仅使用 Python 标准库)。

功能:
  1. 读取 16 位单声道 PCM WAV(采样率 8000~48000Hz,文件不超过 10MB)。
  2. 检测持续超过 0.5 秒的停顿(默认阈值 -40dBFS,可调)。
  3. 按清单 JSON 裁剪片段,自动修剪与停顿重叠超过 0.1 秒的部分。
  4. 按清单顺序拼接,边界做线性渐变过渡,输出合集 WAV 与 JSON 报告。

用法:
  python podcast_concat.py --input in.wav --manifest clips.json \
      [--output output.wav] [--threshold -40]
"""

import argparse
import array
import json
import os
import sys
import wave

MAX_FILE_BYTES = 10 * 1024 * 1024   # 10MB
MIN_RATE = 8000
MAX_RATE = 48000
DEFAULT_THRESHOLD_DB = -40.0
THRESHOLD_MIN_DB = -60.0
THRESHOLD_MAX_DB = -20.0
MIN_PAUSE_SEC = 0.5                 # 持续超过 0.5 秒记为停顿
OVERLAP_TRIM_SEC = 0.1              # 与停顿重叠超过 0.1 秒则修剪
FADE_SEC = 0.010                    # 片段边界渐变时长(10ms)
FULL_SCALE = 32767.0                # 16 位 PCM 满幅


class UserError(Exception):
    """可预期的输入/参数错误,消息直接展示给用户。"""


# ---------------------------------------------------------------- WAV 读取

def read_wav(path):
    """读取并校验 WAV,返回 (采样率, array('h') 样本, 时长秒)。"""
    if not os.path.isfile(path):
        raise UserError(f"输入文件不存在: {path}")
    size = os.path.getsize(path)
    if size > MAX_FILE_BYTES:
        raise UserError(
            f"文件大小 {size} 字节超过 10MB 限制({MAX_FILE_BYTES} 字节): {path}")
    try:
        wf = wave.open(path, "rb")
    except wave.Error as exc:
        raise UserError(f"无法解析 WAV 文件(仅接受 16 位单声道 PCM): {exc}")
    except EOFError:
        raise UserError("无法解析 WAV 文件(仅接受 16 位单声道 PCM): 文件不完整")
    with wf:
        channels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        rate = wf.getframerate()
        nframes = wf.getnframes()
        if channels != 1 or sampwidth != 2:
            raise UserError(
                f"仅接受 16 位单声道 PCM WAV(当前文件为 {channels} 声道、"
                f"{sampwidth * 8} 位)")
        if not (MIN_RATE <= rate <= MAX_RATE):
            raise UserError(
                f"采样率 {rate}Hz 超出允许范围 [{MIN_RATE}, {MAX_RATE}]Hz")
        raw = wf.readframes(nframes)
    samples = array.array("h")
    samples.frombytes(raw)
    if sys.byteorder == "big":
        samples.byteswap()
    duration = len(samples) / rate
    return rate, samples, duration


# ---------------------------------------------------------------- 停顿检测

def detect_pauses(samples, rate, threshold_db):
    """识别连续低于阈值且持续超过 0.5 秒的样本段,按开始时间升序返回。"""
    amp_limit = FULL_SCALE * 10.0 ** (threshold_db / 20.0)
    min_run = MIN_PAUSE_SEC * rate  # 严格大于该样本数才记为停顿
    pauses = []
    run_start = -1
    n = len(samples)
    for i in range(n):
        if abs(samples[i]) <= amp_limit:
            if run_start < 0:
                run_start = i
        else:
            if run_start >= 0:
                if i - run_start > min_run:
                    pauses.append((run_start / rate, i / rate))
                run_start = -1
    if run_start >= 0 and n - run_start > min_run:
        pauses.append((run_start / rate, n / rate))
    return pauses


# ---------------------------------------------------------------- 清单校验

def load_manifest(path, duration):
    """读取并校验片段清单,返回 [(name, start, end), ...]。"""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except OSError as exc:
        raise UserError(f"无法读取清单文件 {path}: {exc}")
    except json.JSONDecodeError as exc:
        raise UserError(f"清单文件不是合法 JSON: {exc}")
    if not isinstance(data, list):
        raise UserError("清单 JSON 须为数组,每项含 name/start/end")

    clips = []
    for idx, item in enumerate(data):
        if not isinstance(item, dict):
            raise UserError(f"清单第 {idx} 项须为对象,含 name/start/end 字段")
        for field in ("name", "start", "end"):
            if field not in item:
                raise UserError(f"清单第 {idx} 项缺少必需字段 '{field}'")
        name = item["name"]
        if not isinstance(name, str) or not name:
            raise UserError(f"清单第 {idx} 项字段 'name' 须为非空字符串")
        start, end = item["start"], item["end"]
        for field, value in (("start", start), ("end", end)):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise UserError(
                    f"片段 '{name}' 字段 '{field}' 须为数字(秒,可含小数)")
        if not start < end:
            raise UserError(
                f"片段 '{name}' 须满足 start<end(当前 start={start}, end={end})")
        if start < 0:
            raise UserError(f"片段 '{name}' 的 start={start} 小于 0")
        if end > duration:
            raise UserError(
                f"片段 '{name}' 的 end={end} 超出录音时长 "
                f"{duration:.3f} 秒,整份清单被拒绝")
        clips.append((name, float(start), float(end)))
    return clips


# ---------------------------------------------------------------- 修剪

def trim_clip(start, end, pauses):
    """把与停顿重叠超过 0.1 秒的片段修剪到停顿边界,返回 (新起点, 新终点, 警告列表)。"""
    new_start, new_end = start, end
    warnings = []
    for p_start, p_end in pauses:
        overlap = min(new_end, p_end) - max(new_start, p_start)
        if overlap <= OVERLAP_TRIM_SEC:
            continue
        trimmed = False
        if p_start <= new_start < p_end:      # 片段起点落在停顿内
            new_start = p_end
            trimmed = True
        if p_start < new_end <= p_end:        # 片段终点落在停顿内
            new_end = p_start
            trimmed = True
        if not trimmed:                        # 停顿完全位于片段内部,保留前段
            new_end = p_start
        warnings.append(
            f"与停顿 [{p_start:.3f}, {p_end:.3f}] 重叠 {overlap:.3f} 秒,"
            f"已修剪到停顿边界 [{new_start:.3f}, {new_end:.3f}]")
    return new_start, new_end, warnings


# ---------------------------------------------------------------- 拼接输出

def build_output(samples, rate, spans):
    """按顺序拼接 (起始样本, 结束样本) 列表,各片段首尾做线性渐变。"""
    fade_n = max(1, int(rate * FADE_SEC))
    out = array.array("h")
    for s0, s1 in spans:
        seg = array.array("h", samples[s0:s1])
        n = len(seg)
        f = min(fade_n, n // 2)
        for i in range(f):
            gain = i / f
            seg[i] = int(seg[i] * gain)
            seg[n - 1 - i] = int(seg[n - 1 - i] * gain)
        out.extend(seg)
    return out


def write_wav(path, rate, samples):
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(samples.tobytes())


def report_path_for(output_path):
    stem, _ = os.path.splitext(output_path)
    return stem + ".report.json"


# ---------------------------------------------------------------- 主流程

def parse_args(argv):
    parser = argparse.ArgumentParser(
        description="播客精彩片段合集拼接工具:从长录音中裁剪片段并拼接为合集 WAV。")
    parser.add_argument("--input", required=True,
                        help="输入 WAV 文件(16 位单声道 PCM,8000~48000Hz,≤10MB)")
    parser.add_argument("--manifest", required=True,
                        help="片段清单 JSON 路径,每项含 name/start/end(秒)")
    parser.add_argument("--output", default="output.wav",
                        help="输出 WAV 路径(默认 output.wav),已存在则拒绝写入")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD_DB,
                        help="停顿阈值(dBFS),合法范围 -60 至 -20,默认 -40")
    args = parser.parse_args(argv)
    if not (THRESHOLD_MIN_DB <= args.threshold <= THRESHOLD_MAX_DB):
        parser.error(
            f"停顿阈值 {args.threshold}dBFS 越界,合法范围为 "
            f"{THRESHOLD_MIN_DB:.0f} 至 {THRESHOLD_MAX_DB:.0f}")
    return args


def run(args):
    rate, samples, duration = read_wav(args.input)
    clips = load_manifest(args.manifest, duration)
    pauses = detect_pauses(samples, rate, args.threshold)

    output_path = args.output
    report_path = report_path_for(output_path)
    for target in (output_path, report_path):
        if os.path.exists(target):
            raise UserError(f"目标文件已存在,拒绝写入: {target}")

    segments = []
    spans = []
    cursor = 0  # 输出时间轴上的样本位置
    for name, start, end in clips:
        new_start, new_end, warnings = trim_clip(start, end, pauses)
        s0 = round(new_start * rate)
        s1 = round(new_end * rate)
        entry = {
            "name": name,
            "start": round(start, 3),
            "end": round(end, 3),
            "output_start": None,
            "output_end": None,
            "dropped": False,
            "warnings": warnings,
        }
        if new_end - new_start <= 0 or s1 - s0 <= 0:
            entry["dropped"] = True
            entry["warnings"].append(
                "修剪后长度不大于 0,已从合集中剔除")
        else:
            entry["output_start"] = round(cursor / rate, 3)
            cursor += s1 - s0
            entry["output_end"] = round(cursor / rate, 3)
            spans.append((s0, s1))
        segments.append(entry)

    out_samples = build_output(samples, rate, spans)

    report = {
        "input_wav": args.input,
        "output_wav": output_path,
        "sample_rate": rate,
        "duration": round(duration, 3),
        "threshold_dbfs": args.threshold,
        "pauses": [{"start": round(p0, 3), "end": round(p1, 3)}
                   for p0, p1 in pauses],
        "segments": segments,
    }

    write_wav(output_path, rate, out_samples)
    with open(report_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
        fh.write("\n")

    kept = sum(1 for s in segments if not s["dropped"])
    print(f"完成: {kept} 个片段拼接为 {output_path}"
          f"(时长 {len(out_samples) / rate:.3f} 秒),报告见 {report_path}")
    return 0


def main(argv=None):
    args = parse_args(argv)
    try:
        return run(args)
    except UserError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
