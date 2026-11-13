#!/usr/bin/env python3
"""航线广播录音裁剪工具。

按秒区间截取单声道 16 位 PCM WAV，首尾加线性音量渐变，
输出裁剪后的 WAV 与 JSON 处理报告（report.json）。

仅使用 Python 标准库，无第三方依赖。
"""
import argparse
import json
import os
import sys
import wave
from array import array

REPORT_NAME = "report.json"
MAX_S16 = 32767
MIN_S16 = -32768


class ProcessingError(Exception):
    """可读的业务处理错误（不打印堆栈）。"""


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="audio_clip",
        description="航线广播录音裁剪工具：截取秒区间并添加线性淡入淡出，生成 JSON 报告。",
    )
    parser.add_argument("--input", required=True,
                        help="输入 WAV 文件路径（仅支持单声道 16 位 PCM）")
    parser.add_argument("--output-dir", required=True,
                        help="输出目录（必须已存在且可写，工具不会自动创建）")
    parser.add_argument("--start", type=float, required=True,
                        help="裁剪起始秒（含），必须 >= 0 且早于结束秒")
    parser.add_argument("--end", type=float, required=True,
                        help="裁剪结束秒（不含），不得超过音频总时长")
    parser.add_argument("--fade-in", type=float, required=True,
                        help="淡入时长（秒），>= 0")
    parser.add_argument("--fade-out", type=float, required=True,
                        help="淡出时长（秒），>= 0")
    parser.add_argument("--output-name", default=None,
                        help="输出 WAV 文件名（默认：<输入名去扩展名>_clip.wav）")
    return parser.parse_args(argv)


def check_output_dir(out_dir):
    """输出目录必须已存在且可写；不创建任何文件。"""
    if not os.path.exists(out_dir):
        raise ProcessingError(f"无法写入目标输出目录：目录不存在（{out_dir}）")
    if not os.path.isdir(out_dir):
        raise ProcessingError(f"无法写入目标输出目录：不是目录（{out_dir}）")
    if not os.access(out_dir, os.W_OK):
        raise ProcessingError(f"无法写入目标输出目录：目录不可写（{out_dir}）")


def read_wav(path):
    """读取并校验 WAV，返回 (params_dict, samples array('h'))。"""
    if not os.path.isfile(path):
        raise ProcessingError(f"无法读取WAV文件：输入不存在（{path}）")
    try:
        ctx = wave.open(path, "rb")
    except (wave.Error, EOFError, OSError) as exc:
        raise ProcessingError(f"无法读取WAV文件：文件损坏或不是有效的WAV（{path}）") from exc
    with ctx:
        channels = ctx.getnchannels()
        sampwidth = ctx.getsampwidth()
        comptype = ctx.getcomptype()
        framerate = ctx.getframerate()
        nframes = ctx.getnframes()
        if comptype != "NONE":
            raise ProcessingError(f"仅支持未压缩的PCM WAV，当前压缩类型为 {comptype}")
        if channels != 1:
            raise ProcessingError(f"仅支持单声道WAV，当前输入为 {channels} 声道")
        if sampwidth != 2:
            raise ProcessingError(f"仅支持16位PCM位深，当前输入为 {sampwidth * 8} 位")
        if framerate <= 0:
            raise ProcessingError("WAV采样率无效")
        raw = ctx.readframes(nframes)
    samples = array("h")
    samples.frombytes(raw[: len(raw) - (len(raw) % 2)])
    if sys.byteorder == "big":
        samples.byteswap()
    return {"framerate": framerate, "nframes": nframes}, samples


def validate_range(start, end, duration):
    if start < 0:
        raise ProcessingError(f"裁剪区间无效：起始秒不能为负（{start}）")
    if end <= start:
        raise ProcessingError(
            f"裁剪区间无效：起始秒（{start}）必须早于结束秒（{end}）")
    if end > duration:
        raise ProcessingError(
            f"裁剪区间无效：结束秒（{end}）超出音频总时长（{duration:.3f} 秒）")


def validate_fades(fade_in, fade_out, clip_sec):
    if fade_in < 0:
        raise ProcessingError(f"淡入时长无效：不能为负（{fade_in} 秒）")
    if fade_out < 0:
        raise ProcessingError(f"淡出时长无效：不能为负（{fade_out} 秒）")
    eps = 1e-9
    if fade_in - clip_sec > eps:
        raise ProcessingError(
            f"淡入时长（{fade_in} 秒）超过片段长度（{clip_sec:.3f} 秒）")
    if fade_out - clip_sec > eps:
        raise ProcessingError(
            f"淡出时长（{fade_out} 秒）超过片段长度（{clip_sec:.3f} 秒）")
    if fade_in + fade_out - clip_sec > eps:
        raise ProcessingError(
            f"渐变总长（淡入 {fade_in} + 淡出 {fade_out} = {fade_in + fade_out:.3f} 秒）"
            f"超过片段长度（{clip_sec:.3f} 秒）")


def apply_fades(samples, fade_in_frames, fade_out_frames):
    """线性渐变：淡入首帧、淡出末帧严格为 0。"""
    out = array("h", samples)
    n = len(out)
    fi = min(fade_in_frames, n)
    fo = min(fade_out_frames, n)
    for i in range(fi):
        factor = i / fi  # i=0 -> 0
        out[i] = _scale(out[i], factor)
    for k in range(fo):
        idx = n - fo + k
        factor = (fo - 1 - k) / (fo - 1) if fo > 1 else 0.0  # k=fo-1 -> 0
        out[idx] = _scale(out[idx], factor)
    return out


def _scale(value, factor):
    v = int(round(value * factor))
    return max(MIN_S16, min(MAX_S16, v))


def write_wav(path, framerate, samples):
    """先写临时文件再原子替换，避免留下半成品 WAV。"""
    tmp = path + ".tmp"
    try:
        with wave.open(tmp, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(framerate)
            w.writeframes(samples.tobytes())
        os.replace(tmp, path)
    except OSError as exc:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        raise ProcessingError(f"无法写入输出WAV（{path}）：{exc}") from exc


def process(args, out_dir):
    """执行裁剪，返回报告字段。失败抛 ProcessingError。"""
    params, samples = read_wav(args.input)
    framerate = params["framerate"]
    duration = params["nframes"] / framerate

    validate_range(args.start, args.end, duration)
    clip_sec = args.end - args.start
    validate_fades(args.fade_in, args.fade_out, clip_sec)

    n_out = int(round(clip_sec * framerate))
    if n_out <= 0:
        raise ProcessingError("裁剪区间过短，换算后样本数为 0")
    start_frame = int(round(args.start * framerate))
    if start_frame + n_out > len(samples):
        raise ProcessingError("裁剪区间超出实际可读取的音频数据")

    clip = samples[start_frame:start_frame + n_out]
    fade_in_frames = int(round(args.fade_in * framerate))
    fade_out_frames = int(round(args.fade_out * framerate))
    result = apply_fades(clip, fade_in_frames, fade_out_frames)

    out_name = args.output_name or (
        os.path.splitext(os.path.basename(args.input))[0] + "_clip.wav")
    out_path = os.path.join(out_dir, out_name)
    write_wav(out_path, framerate, result)

    return {
        "output_file": out_name,
        "original_duration_sec": duration,
        "clipped_duration_sec": n_out / framerate,
        "fade_in_sec": args.fade_in,
        "fade_out_sec": args.fade_out,
    }


def build_report(args):
    return {
        "status": "failed",
        "input_file": os.path.basename(args.input),
        "output_file": None,
        "original_duration_sec": None,
        "clipped_duration_sec": None,
        "fade_in_sec": args.fade_in,
        "fade_out_sec": args.fade_out,
        "error": None,
    }


def try_write_report(out_dir, report):
    """目录可写时写出报告；失败时明确告知。返回是否写出。"""
    path = os.path.join(out_dir, REPORT_NAME)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        return True
    except OSError:
        return False


def main(argv=None):
    args = parse_args(argv)

    try:
        check_output_dir(args.output_dir)
    except ProcessingError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        print("处理未执行，未生成任何文件；处理报告也未能写出。", file=sys.stderr)
        return 1

    report = build_report(args)
    try:
        result = process(args, args.output_dir)
    except ProcessingError as exc:
        report["error"] = str(exc)
        print(f"处理失败：{exc}", file=sys.stderr)
        if try_write_report(args.output_dir, report):
            print(f"已写出失败报告：{os.path.join(args.output_dir, REPORT_NAME)}",
                  file=sys.stderr)
        else:
            print("无法写出处理报告（输出目录不可写）。", file=sys.stderr)
        return 1

    report.update(result)
    report["status"] = "success"
    report["error"] = None
    if not try_write_report(args.output_dir, report):
        print("警告：处理成功但无法写出处理报告（输出目录不可写）。", file=sys.stderr)
        return 1
    print(f"处理成功：{report['output_file']}，报告见 {REPORT_NAME}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
