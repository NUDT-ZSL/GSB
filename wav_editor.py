#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wav_editor.py —— 16 位 PCM WAV 时间线拼接工具（仅使用 Python 标准库）

功能：
  - 按 JSON 编辑清单把多个 WAV 片段放到毫秒时间线上（可重叠混音、可留静音段）
  - 不同采样率统一到首片段采样率（线性插值重采样），单/双声道互转
  - 片段增益（分贝 dB），重叠区采样值相加并限幅到 [-32768, 32767]，绝不回绕
  - 命名标记点校验（负数 / 超出总时长逐条拒绝并说明原因）
  - 输出：拼接 WAV、标记 JSON、摘要报告 JSON
  - 整单校验失败时不向输出目录写入任何文件，并以非零码退出

用法：
  python -I wav_editor.py <编辑清单.json> <输出目录>
"""

import argparse
import array
import json
import math
import os
import struct
import sys
import wave

# ---------------------------------------------------------------- 常量
SAMPLE_MIN = -32768
SAMPLE_MAX = 32767
GAIN_MIN_DB = -60.0
GAIN_MAX_DB = 12.0
SUPPORTED_CHANNELS = (1, 2)

DEFAULT_WAV_NAME = "output.wav"
DEFAULT_MARKERS_NAME = "markers.json"
DEFAULT_REPORT_NAME = "report.json"
# 空清单（无任何片段）没有可参考的采样率，写 0 帧 WAV 时使用的兜底规格
EMPTY_FALLBACK_RATE = 44100
EMPTY_FALLBACK_CHANNELS = 1


# ---------------------------------------------------------------- 基础工具
def is_finite_number(value):
    """布尔值不算数值；要求 int/float 且有限（排除 NaN / Infinity）。"""
    if isinstance(value, bool):
        return False
    if not isinstance(value, (int, float)):
        return False
    return math.isfinite(value)


def ms_to_frames(time_ms, sample_rate):
    """毫秒 -> 帧数，四舍五入。"""
    return int(round(time_ms * sample_rate / 1000.0))


def clamp_sample(value):
    """限幅到 16 位有符号整数范围，防止溢出回绕。"""
    if value > SAMPLE_MAX:
        return SAMPLE_MAX
    if value < SAMPLE_MIN:
        return SAMPLE_MIN
    # 加 0.5 对正数做四舍五入，负数截断（向零取整），配合外层限幅
    return int(value + (0.5 if value >= 0 else -0.5))


# ---------------------------------------------------------------- WAV 读取
def read_wav_16bit(path):
    """
    读取 16 位 PCM WAV。
    返回 dict: sample_rate, channels, n_frames, samples(array('h'), 交错存储)
    文件缺失 / 非 WAV / 非 16 位 PCM / 声道不支持时抛出 ValueError(中文原因)。
    """
    if not os.path.isfile(path):
        raise ValueError("文件不存在: %s" % path)
    try:
        wf = wave.open(path, "rb")
    except (wave.Error, EOFError, struct.error) as exc:
        raise ValueError("无法解析为 WAV 文件: %s（%s）" % (path, exc))
    try:
        channels = wf.getnchannels()
        sample_width = wf.getsampwidth()
        sample_rate = wf.getframerate()
        n_frames = wf.getnframes()
        compression = wf.getcomptype()
        if compression != "NONE":
            raise ValueError(
                "仅支持未压缩 PCM WAV，文件 %s 的压缩类型为 %s" % (path, compression)
            )
        if sample_width != 2:
            raise ValueError(
                "仅支持 16 位 PCM WAV，文件 %s 的采样宽度为 %d 字节(%d 位)"
                % (path, sample_width, sample_width * 8)
            )
        if channels not in SUPPORTED_CHANNELS:
            raise ValueError(
                "仅支持单/双声道 WAV，文件 %s 的声道数为 %d" % (path, channels)
            )
        raw = wf.readframes(n_frames)
    finally:
        wf.close()

    samples = array.array("h")
    samples.frombytes(raw)
    if sys.byteorder != "little":  # WAV 为小端，非小端平台需交换
        samples.byteswap()
    return {
        "path": path,
        "sample_rate": sample_rate,
        "channels": channels,
        "n_frames": n_frames,
        "samples": samples,
    }


# ---------------------------------------------------------------- 重采样 / 声道转换
def resample_channel(src, src_rate, dst_rate):
    """对单声道 array('h') 做线性插值重采样，返回 array('h')。"""
    n_in = len(src)
    if n_in == 0:
        return array.array("h")
    if src_rate == dst_rate:
        return array.array("h", src)
    n_out = max(1, int(round(n_in * dst_rate / src_rate)))
    out = array.array("h", [0]) * n_out
    ratio = src_rate / dst_rate
    for i in range(n_out):
        src_pos = i * ratio
        i0 = int(src_pos)
        frac = src_pos - i0
        if i0 + 1 < n_in:
            v = src[i0] * (1.0 - frac) + src[i0 + 1] * frac
        else:
            v = float(src[n_in - 1])
        out[i] = clamp_sample(v)
    return out


def convert_clip(clip_info, target_rate, target_channels, gain_linear):
    """
    把已读取的片段统一到目标采样率/声道数，并应用增益。
    返回 array('h')，按目标声道交错存储。
    """
    src = clip_info["samples"]
    src_channels = clip_info["channels"]
    n_frames = clip_info["n_frames"]

    # 1) 拆分为各声道
    channels = []
    for ch in range(src_channels):
        channels.append(array.array("h", src[ch::src_channels]))

    # 2) 逐声道重采样
    channels = [resample_channel(c, clip_info["sample_rate"], target_rate)
                for c in channels]
    n_out = len(channels[0])

    # 3) 声道数统一
    if src_channels == target_channels:
        pass
    elif src_channels == 2 and target_channels == 1:
        # 立体声 -> 单声道：左右平均
        mono = array.array("h", [0]) * n_out
        left, right = channels
        for i in range(n_out):
            mono[i] = clamp_sample((left[i] + right[i]) / 2.0)
        channels = [mono]
    elif src_channels == 1 and target_channels == 2:
        # 单声道 -> 立体声：复制到两个声道
        channels = [channels[0], array.array("h", channels[0])]
    else:  # 读取阶段已拦截，理论不可达
        raise ValueError("不支持的声道转换: %d -> %d"
                         % (src_channels, target_channels))

    # 4) 应用增益并交错
    interleaved = array.array("h", [0]) * (n_out * target_channels)
    idx = 0
    for frame in range(n_out):
        for ch in range(target_channels):
            interleaved[idx] = clamp_sample(
                channels[ch][frame] * gain_linear
            )
            idx += 1
    return interleaved


# ---------------------------------------------------------------- 清单校验
def validate_manifest(manifest, manifest_dir):
    """
    全量校验编辑清单，返回 (config, errors)。
    config: dict(output_names, clips[(path,start_ms,duration_ms,gain_db,index)],
                 markers[(name,time_ms,index)])
    errors: 全部错误字符串列表（字段级错误 + 文件级错误），非空即整单拒绝。
    """
    errors = []

    if not isinstance(manifest, dict):
        return None, ["清单顶层必须是 JSON 对象"]

    out_block = manifest.get("output", {})
    if not isinstance(out_block, dict):
        errors.append("字段 output 必须是对象")
        out_block = {}
    wav_name = out_block.get("wav", DEFAULT_WAV_NAME)
    markers_name = out_block.get("markers", DEFAULT_MARKERS_NAME)
    report_name = out_block.get("report", DEFAULT_REPORT_NAME)
    for fname, fld in ((wav_name, "output.wav"),
                       (markers_name, "output.markers"),
                       (report_name, "output.report")):
        if not isinstance(fname, str) or not fname.strip():
            errors.append("字段 %s 必须是非空文件名字符串" % fld)

    # ---- 片段字段校验（时间与增益：有限数值、时间非负、增益范围）
    raw_clips = manifest.get("clips", [])
    if not isinstance(raw_clips, list):
        errors.append("字段 clips 必须是数组")
        raw_clips = []

    clips = []
    cursor_ms = 0.0  # 未指定 start 时，紧接上一片段末尾
    for idx, clip in enumerate(raw_clips):
        prefix = "clips[%d]" % idx
        if not isinstance(clip, dict):
            errors.append("%s 必须是对象" % prefix)
            continue

        path = clip.get("path")
        if not isinstance(path, str) or not path.strip():
            errors.append("%s.path 缺失或不是字符串" % prefix)
            path = None
        else:
            path = os.path.normpath(os.path.join(manifest_dir, path))

        has_start = "start_ms" in clip
        start_ms = clip.get("start_ms")
        if has_start:
            if not is_finite_number(start_ms):
                errors.append("%s.start_ms 必须是有限数值" % prefix)
                start_ok = False
            elif start_ms < 0:
                errors.append("%s.start_ms 必须为非负数，实际为 %r"
                              % (prefix, start_ms))
                start_ok = False
            else:
                start_ok = True
        else:
            start_ms = cursor_ms
            start_ok = True

        has_dur = "duration_ms" in clip
        duration_ms = clip.get("duration_ms")
        if has_dur:
            if not is_finite_number(duration_ms):
                errors.append("%s.duration_ms 必须是有限数值" % prefix)
                dur_ok = False
            elif duration_ms < 0:
                errors.append("%s.duration_ms 必须为非负数，实际为 %r"
                              % (prefix, duration_ms))
                dur_ok = False
            else:
                dur_ok = True
        else:
            dur_ok = True  # 缺省取源文件全长，读取后决定

        gain_db = clip.get("gain_db", 0.0)
        if not is_finite_number(gain_db):
            errors.append("%s.gain_db 必须是有限数值" % prefix)
            gain_ok = False
        elif not (GAIN_MIN_DB <= gain_db <= GAIN_MAX_DB):
            errors.append(
                "%s.gain_db 超出允许范围 [%g, %g] dB，实际为 %r"
                % (prefix, GAIN_MIN_DB, GAIN_MAX_DB, gain_db)
            )
            gain_ok = False
        else:
            gain_ok = True

        if path is not None and start_ok and dur_ok and gain_ok:
            clips.append({
                "index": idx,
                "path": path,
                "start_ms": float(start_ms),
                "duration_ms": float(duration_ms) if has_dur else None,
                "gain_db": float(gain_db),
            })
            cursor_ms = float(start_ms) + (
                float(duration_ms) if has_dur else 0.0
            )

    # ---- 标记字段校验
    raw_markers = manifest.get("markers", [])
    if not isinstance(raw_markers, list):
        errors.append("字段 markers 必须是数组")
        raw_markers = []
    markers = []
    for idx, mk in enumerate(raw_markers):
        prefix = "markers[%d]" % idx
        if not isinstance(mk, dict):
            errors.append("%s 必须是对象" % prefix)
            continue
        name = mk.get("name")
        if not isinstance(name, str) or not name.strip():
            errors.append("%s.name 缺失或不是非空字符串" % prefix)
            name = None
        time_ms = mk.get("time_ms")
        if not is_finite_number(time_ms):
            errors.append("%s.time_ms 必须是有限数值" % prefix)
            time_ok = False
        else:
            time_ok = True
        if name is not None and time_ok:
            markers.append({"index": idx, "name": name,
                            "time_ms": float(time_ms)})

    config = {
        "wav_name": wav_name,
        "markers_name": markers_name,
        "report_name": report_name,
        "clips": clips,
        "markers": markers,
    }

    # ---- 字段全部合法后再检查源文件（整单拒绝，错误中指明文件与原因）
    if not errors:
        for clip in clips:
            try:
                clip["audio"] = read_wav_16bit(clip["path"])
            except ValueError as exc:
                errors.append(str(exc))

    return config, errors


# ---------------------------------------------------------------- 时间线混音
def build_timeline(config):
    """
    执行重采样、增益、时间线放置与重叠相加。
    返回 (mixed(list[float]), target_rate, target_channels, total_frames,
          clip_records)
    """
    clips = config["clips"]
    clip_records = []

    if not clips:
        return [], None, None, 0, clip_records

    first = clips[0]["audio"]
    target_rate = first["sample_rate"]
    target_channels = first["channels"]

    # 先统一处理每个片段，得到放置信息
    placed = []
    for clip in clips:
        audio = clip["audio"]
        gain_linear = 10.0 ** (clip["gain_db"] / 20.0)

        if clip["duration_ms"] is None:
            src_frames = audio["n_frames"]
        else:
            dur_frames_src = ms_to_frames(clip["duration_ms"],
                                          audio["sample_rate"])
            src_frames = min(audio["n_frames"], max(0, dur_frames_src))

        trimmed = {
            "sample_rate": audio["sample_rate"],
            "channels": audio["channels"],
            "n_frames": src_frames,
            "samples": array.array(
                "h", audio["samples"][:src_frames * audio["channels"]]
            ),
        }
        data = convert_clip(trimmed, target_rate, target_channels,
                            gain_linear)
        n_out_frames = len(data) // target_channels
        start_frame = ms_to_frames(clip["start_ms"], target_rate)
        placed.append((start_frame, data, n_out_frames, clip))
        clip_records.append({
            "index": clip["index"],
            "path": clip["path"],
            "start_ms": clip["start_ms"],
            "source_rate": audio["sample_rate"],
            "source_channels": audio["channels"],
            "gain_db": clip["gain_db"],
            "placed_frames": n_out_frames,
        })

    total_frames = max((s + n for s, _, n, _ in placed), default=0)
    # float64 累加，避免整数相加溢出；静音段天然为 0
    mixed = [0.0] * (total_frames * target_channels)
    for start_frame, data, n_out_frames, _clip in placed:
        offset = start_frame * target_channels
        for i, value in enumerate(data):
            mixed[offset + i] += float(value)

    return mixed, target_rate, target_channels, total_frames, clip_records


def encode_samples(mixed, channels):
    """float 采样列表 -> 限幅后的 array('h')。"""
    out = array.array("h", [0]) * len(mixed)
    for i, value in enumerate(mixed):
        out[i] = clamp_sample(value)
    return out


# ---------------------------------------------------------------- 标记处理
def process_markers(markers, total_frames, sample_rate):
    """
    逐条校验标记时间：负数 / 超出总时长拒绝，其余保留。
    总帧数为 0（空时间线）时，所有非空标记一律记为越界。
    返回 (valid_markers, rejected)
    """
    valid, rejected = [], []
    if sample_rate:
        total_ms = total_frames * 1000.0 / sample_rate
    else:
        total_ms = 0.0
    for mk in markers:
        t = mk["time_ms"]
        if t < 0:
            rejected.append({
                "index": mk["index"], "name": mk["name"], "time_ms": t,
                "reason": "标记时间为负数(%g ms)" % t,
            })
        elif total_frames == 0 or t > total_ms:
            rejected.append({
                "index": mk["index"], "name": mk["name"], "time_ms": t,
                "reason": "标记越界：标记时间 %g ms 超出输出总时长 %g ms"
                          % (t, total_ms),
            })
        else:
            valid.append({
                "name": mk["name"],
                "time_ms": t,
                "frame": ms_to_frames(t, sample_rate),
            })
    return valid, rejected


# ---------------------------------------------------------------- 输出（原子写，覆盖旧文件）
def write_wav(path, samples, sample_rate, channels):
    tmp = path + ".tmp"
    with wave.open(tmp, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate if sample_rate is not None else 0)
        wf.setcomptype("NONE", "not compressed")
        wf.writeframes(samples.tobytes())
    os.replace(tmp, path)


def write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# ---------------------------------------------------------------- 主流程
def run(manifest_path, output_dir):
    """返回 (exit_code, report_or_errors)。成功时写三份文件。"""
    if not os.path.isfile(manifest_path):
        return 1, ["清单文件不存在: %s" % manifest_path]
    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return 1, ["清单 JSON 解析失败: %s" % exc]
    except OSError as exc:
        return 1, ["清单文件无法读取: %s" % exc]

    manifest_dir = os.path.dirname(os.path.abspath(manifest_path))
    config, errors = validate_manifest(manifest, manifest_dir)
    if errors:
        return 1, errors  # 整单拒绝：不创建/写入任何输出文件

    mixed, rate, channels, total_frames, clip_records = build_timeline(config)

    if total_frames == 0:
        samples = array.array("h")
        # 空时间线：沿用片段规格（若有 0 时长片段），否则用兜底规格写 0 帧 WAV
        out_rate = rate if rate is not None else EMPTY_FALLBACK_RATE
        out_channels = (channels if channels is not None
                        else EMPTY_FALLBACK_CHANNELS)
    else:
        samples = encode_samples(mixed, channels)
        out_rate, out_channels = rate, channels

    valid_markers, rejected_markers = process_markers(
        config["markers"], total_frames, out_rate
    )

    total_ms = (total_frames * 1000.0 / out_rate) if out_rate else 0.0
    peak = max((abs(v) for v in samples), default=0)
    os.makedirs(output_dir, exist_ok=True)
    wav_path = os.path.join(output_dir, config["wav_name"])
    markers_path = os.path.join(output_dir, config["markers_name"])
    report_path = os.path.join(output_dir, config["report_name"])

    marker_payload = {
        "sample_rate": out_rate,
        "channels": out_channels,
        "total_frames": total_frames,
        "total_duration_ms": round(total_ms, 6),
        "markers": valid_markers,
    }
    report = {
        "status": "success",
        "output_wav": config["wav_name"],
        "output_markers": config["markers_name"],
        "report_file": config["report_name"],
        "sample_rate": out_rate,
        "channels": out_channels,
        "total_frames": total_frames,
        "total_duration_ms": round(total_ms, 6),
        "clip_count": len(config["clips"]),
        "valid_marker_count": len(valid_markers),
        "rejected_marker_count": len(rejected_markers),
        "peak_abs_sample": int(peak),
        "clips": clip_records,
        "valid_markers": valid_markers,
        "rejected_markers": rejected_markers,
    }

    write_wav(wav_path, samples, out_rate, out_channels)
    write_json(markers_path, marker_payload)
    write_json(report_path, report)
    return 0, report


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="16 位 PCM WAV 时间线拼接与标记工具（标准库实现）"
    )
    parser.add_argument("manifest", help="编辑清单 JSON 路径")
    parser.add_argument("output_dir", help="输出目录（成功后写入三份文件）")
    args = parser.parse_args(argv)

    exit_code, payload = run(args.manifest, args.output_dir)
    if exit_code != 0:
        print("整单拒绝，未生成任何输出文件。错误明细：", file=sys.stderr)
        for item in payload:
            print("  - " + str(item), file=sys.stderr)
    else:
        print("处理成功：")
        print("  WAV    : %s" % os.path.join(args.output_dir,
                                              payload["output_wav"]))
        print("  标记   : %s" % os.path.join(args.output_dir,
                                              payload["output_markers"]))
        print("  报告   : %s" % os.path.join(args.output_dir,
                                              payload["report_file"]))
        print("  总时长 : %g ms | 采样率: %d Hz | 声道: %d | 有效标记: %d"
              % (payload["total_duration_ms"], payload["sample_rate"],
                 payload["channels"], payload["valid_marker_count"]))
        if payload["rejected_markers"]:
            print("  被拒标记：")
            for mk in payload["rejected_markers"]:
                print("    - %s: %s" % (mk["name"], mk["reason"]))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
