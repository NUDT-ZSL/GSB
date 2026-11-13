# -*- coding: utf-8 -*-
"""
WAV 时间线拼接工具（纯 Python 标准库，无第三方依赖）

功能：
  - 读取 JSON 时间线（片段文件路径 / 起始秒 / 淡化秒），把多段
    16-bit/44.1kHz/单声道 PCM WAV 拼成一条音轨
  - 相邻片段重叠区做等功率斜率的线性交叉淡化（淡入 + 淡出），避免爆音
  - 格式不符、文件缺失/不可读、淡化时长不合理、起点重复等异常逐段跳过
  - 时间线非法（负起点、非严格递增）或 JSON 结构缺字段时整体拒绝处理
  - 生成可播放 WAV 与面向业务人员的 JSON 处理报告

用法：
  python -I joiner.py [edit.json] [output.wav] [report.json]
  三个参数均可缺省，缺省依次为当前目录下 edit.json / output.wav / report.json
"""

import argparse
import array
import json
import math
import os
import struct
import sys
import wave

# ---- 目标音频规格：单声道 / 16-bit PCM / 44100Hz ----
TARGET_CHANNELS = 1
TARGET_SAMPLE_WIDTH = 2          # 16-bit = 2 字节
TARGET_RATE = 44100
MAX_16 = 32767
MIN_16 = -32768

STATUS_OK = "成功"
STATUS_SKIP = "跳过"
TOP_LEVEL_KEY = "clips"


class TimelineError(Exception):
    """JSON 结构或时间线本身非法，必须整体拒绝处理。"""


# ----------------------------------------------------------------------
# 1. JSON 读取与校验（结构错误 / 时间线非法时不生成任何输出文件）
# ----------------------------------------------------------------------
def load_and_validate(json_path):
    """读取并校验时间线 JSON，返回规范化后的片段列表。

    每个片段为 dict：file(str) / start(float, 秒) / fade(float, 秒)。
    校验不通过抛 TimelineError。
    """
    try:
        # utf-8-sig 可同时兼容带 BOM 与不带 BOM 的 UTF-8 JSON（Windows 工具常见）
        with open(json_path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except FileNotFoundError:
        raise TimelineError("JSON 文件不存在: %s" % json_path)
    except json.JSONDecodeError as exc:
        raise TimelineError("JSON 语法错误: %s" % exc)
    except OSError as exc:
        raise TimelineError("JSON 文件无法读取: %s" % exc)

    if not isinstance(data, dict) or TOP_LEVEL_KEY not in data:
        raise TimelineError(
            "JSON 顶层缺少片段列表字段 '%s'" % TOP_LEVEL_KEY)
    clips = data[TOP_LEVEL_KEY]
    if not isinstance(clips, list):
        raise TimelineError(
            "JSON 顶层字段 '%s' 必须是片段列表" % TOP_LEVEL_KEY)

    required = ("file", "start", "fade")
    cleaned = []
    for idx, clip in enumerate(clips):
        if not isinstance(clip, dict):
            raise TimelineError("第 %d 个片段必须是 JSON 对象" % (idx + 1))
        for field in required:
            if field not in clip:
                raise TimelineError(
                    "第 %d 个片段缺少必需字段 '%s'" % (idx + 1, field))
            if clip[field] is None:
                raise TimelineError(
                    "第 %d 个片段字段 '%s' 不能为空" % (idx + 1, field))

        path = clip["file"]
        if not isinstance(path, str) or not path.strip():
            raise TimelineError(
                "第 %d 个片段的 file 必须是非空字符串" % (idx + 1))

        start = _as_number(clip["start"], "start", idx)
        fade = _as_number(clip["fade"], "fade", idx)
        if start < 0:
            raise TimelineError(
                "第 %d 个片段起始秒为负数(%s)，时间线非法"
                % (idx + 1, start))
        if not math.isfinite(start) or not math.isfinite(fade):
            raise TimelineError(
                "第 %d 个片段的时间字段必须是有限数值" % (idx + 1))
        if fade < 0:
            raise TimelineError(
                "第 %d 个片段淡化秒为负数(%s)，时间线非法" % (idx + 1, fade))

        cleaned.append({"file": path, "start": float(start),
                        "fade": float(fade)})

    # 起始秒必须非负且严格递增；起点相同的片段标记后由处理阶段跳过
    # （见需求 14：同起点非首片段视为无效并跳过，而非整体拒绝）
    prev = None
    for idx, clip in enumerate(cleaned):
        if prev is not None:
            if clip["start"] < prev:
                raise TimelineError(
                    "第 %d 个片段起始秒(%s)不大于其前一片段(%s)，"
                    "起始秒必须严格递增" % (idx + 1, clip["start"], prev))
            if clip["start"] == prev:
                clip["_duplicate_start"] = True
        prev = clip["start"]

    return cleaned


def _as_number(value, field, idx):
    """bool 是 int 的子类，显式拒绝；字符串等非数值同样拒绝。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TimelineError(
            "第 %d 个片段字段 '%s' 必须是数值，实际为 %s"
            % (idx + 1, field, type(value).__name__))
    return float(value)


# ----------------------------------------------------------------------
# 2. 单段 WAV 读取与格式校验
# ----------------------------------------------------------------------
def read_clip(wav_path):
    """读取 WAV，返回 (samples: array('h'), 校验错误原因 or None)。

    仅接受单声道 / 16-bit PCM / 44100Hz，否则返回 (None, 原因)。
    """
    if not os.path.exists(wav_path):
        return None, "片段文件不存在: %s" % wav_path
    if not os.path.isfile(wav_path):
        return None, "片段路径不是文件: %s" % wav_path
    try:
        with wave.open(wav_path, "rb") as wf:
            channels = wf.getnchannels()
            width = wf.getsampwidth()
            rate = wf.getframerate()
            frames = wf.getnframes()
            raw = wf.readframes(frames)
    except (wave.Error, EOFError, struct.error) as exc:
        return None, "WAV 文件无法解析: %s（%s）" % (wav_path, exc)
    except OSError as exc:
        return None, "WAV 文件无法读取: %s（%s）" % (wav_path, exc)

    bad = []
    if channels != TARGET_CHANNELS:
        bad.append("声道数=%d（要求单声道）" % channels)
    if width != TARGET_SAMPLE_WIDTH:
        bad.append("位深=%dbit（要求16-bit）" % (width * 8))
    if rate != TARGET_RATE:
        bad.append("采样率=%dHz（要求44100Hz）" % rate)
    if bad:
        return None, "格式不匹配: " + "，".join(bad)

    try:
        samples = array.array("h")
        samples.frombytes(raw)
    except (ValueError, EOFError) as exc:
        return None, "WAV 样本数据损坏: %s（%s）" % (wav_path, exc)

    if len(samples) == 0:
        return None, "片段不包含任何音频样本: %s" % wav_path
    return samples, None


# ----------------------------------------------------------------------
# 3. 混音：按绝对时间线摆放，重叠区交叉淡化
# ----------------------------------------------------------------------
def mix(clips_ready):
    """clips_ready: [(clip_meta, samples), ...]，已按起始秒排序。

    返回 (mix_buf: array('q'), records 淡化信息列表)。
    混音使用 64-bit 整数累加，最后写盘前再夹到 16-bit 范围，避免中间溢出。
    """
    first_start = clips_ready[0][0]["start"]
    total_len = 0
    placements = []   # (输出起始样本, 输出结束样本, 样本数组, clip_meta)

    for meta, samples in clips_ready:
        pos = int(round((meta["start"] - first_start) * TARGET_RATE))
        end = pos + len(samples)
        placements.append((pos, end, samples, meta))
        total_len = max(total_len, end)

    mix_buf = array.array("q", [0]) * total_len
    fade_records = []

    for order, (pos, end, samples, meta) in enumerate(placements):
        requested_frames = int(round(meta["fade"] * TARGET_RATE))
        applied = 0
        note = ""
        prev_end = placements[order - 1][1] if order > 0 else None

        # 仅当与前一有效片段实际重叠、且请求淡化样本数 > 0 时才可能淡化
        overlap = prev_end - pos if prev_end is not None else 0
        if requested_frames > 0 and overlap <= 0:
            note = ("请求淡化 %.3f 秒但与前一片段无时间重叠，"
                    "该处未做淡化" % meta["fade"])
        elif requested_frames > 0 and requested_frames > overlap:
            note = ("请求淡化 %.3f 秒(%d 样本)超出片段可承受的重叠范围 "
                    "%d 样本(%.3f 秒)，该处未做淡化"
                    % (meta["fade"], requested_frames, overlap,
                       overlap / TARGET_RATE))
        elif requested_frames > 0:
            applied = requested_frames

        if order == 0 and requested_frames > 0:
            note = "首个片段前无相邻片段，淡化参数不生效"
            applied = 0

        if applied > 0:
            # ---- 交叉淡化区：当前片段淡入 k 0->1，与已写入内容叠加 ----
            # 非淡化区（j >= applied）增益恒为 1，非淡化样本与源片段一致
            for j in range(applied):
                k = (j + 1) / (applied + 1)   # 0 < k < 1，线性平滑斜率
                mix_buf[pos + j] += int(round(samples[j] * k))
            for j in range(applied, len(samples)):
                mix_buf[pos + j] += samples[j]

            # 前一片段在同一重叠区做对称淡出 1->0，
            # 两端均为连续斜率，边界无突变尖峰、无爆音
            prev_pos, prev_end_p, prev_samples, _ = placements[order - 1]
            tail_start = prev_end_p - applied
            for j in range(applied):
                k = (j + 1) / (applied + 1)
                target_idx = prev_pos + (tail_start - prev_pos) + j
                original = prev_samples[len(prev_samples) - applied + j]
                mix_buf[target_idx] = mix_buf[target_idx] - original \
                    + int(round(original * (1.0 - k)))
        else:
            # 不做淡化：原样写入，重叠区直接叠加
            for j, sample in enumerate(samples):
                mix_buf[pos + j] += sample

        fade_records.append({
            "requested_fade_seconds": round(meta["fade"], 3),
            "applied_fade_seconds": round(applied / TARGET_RATE, 3),
            "note": note,
        })

    return mix_buf, fade_records


def _clamp16(value):
    if value > MAX_16:
        return MAX_16
    if value < MIN_16:
        return MIN_16
    return value


def write_wav(path, mix_buf):
    """把 64-bit 累加缓冲夹值后写成 单声道/16-bit/44100Hz PCM WAV。"""
    out = array.array("h", (_clamp16(v) for v in mix_buf))
    with wave.open(path, "wb") as wf:
        wf.setnchannels(TARGET_CHANNELS)
        wf.setsampwidth(TARGET_SAMPLE_WIDTH)
        wf.setframerate(TARGET_RATE)
        wf.writeframes(out.tobytes())
    return len(out)


# ----------------------------------------------------------------------
# 4. 报告
# ----------------------------------------------------------------------
def build_report(clip_records, total_frames, no_usable):
    duration = total_frames / TARGET_RATE if total_frames is not None else 0.0
    return {
        "total_duration_seconds": ("%.3f" % duration),
        "total_frames": total_frames if total_frames is not None else 0,
        "sample_rate": TARGET_RATE,
        "channels": TARGET_CHANNELS,
        "sample_width_bits": TARGET_SAMPLE_WIDTH * 8,
        "no_usable_clip": no_usable,
        "clips": clip_records,
    }


def write_report(path, report):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)


# ----------------------------------------------------------------------
# 5. 主流程
# ----------------------------------------------------------------------
def main(argv=None):
    parser = argparse.ArgumentParser(
        description="按 JSON 时间线拼接 WAV 片段并生成处理报告")
    parser.add_argument("json_path", nargs="?", default="edit.json",
                        help="时间线 JSON（默认: ./edit.json）")
    parser.add_argument("wav_path", nargs="?", default="output.wav",
                        help="输出 WAV（默认: ./output.wav）")
    parser.add_argument("report_path", nargs="?", default="report.json",
                        help="输出报告 JSON（默认: ./report.json）")
    args = parser.parse_args(argv)

    # ---- 阶段 A：结构与时间线校验，失败即整体拒绝，不产生任何文件 ----
    try:
        clips = load_and_validate(args.json_path)
    except TimelineError as exc:
        print("[错误] 时间线校验失败，已终止处理且不生成任何输出文件: %s"
              % exc, file=sys.stderr)
        return 2

    base_dir = os.path.dirname(os.path.abspath(args.json_path))

    clip_records = []
    ready = []          # (meta, samples)
    for idx, meta in enumerate(clips):
        record = {
            "index": idx + 1,
            "file": meta["file"],
            "start_seconds": round(meta["start"], 3),
            "requested_fade_seconds": round(meta["fade"], 3),
            "status": STATUS_SKIP,
            "reason": "",
            "applied_fade_seconds": 0.0,
            "note": "",
        }

        if meta.get("_duplicate_start"):
            record["reason"] = (
                "起始秒 %s 与前一片段相同，按时间线视为无效片段"
                % meta["start"])
            clip_records.append(record)
            print("[跳过] 片段%d %s —— %s"
                  % (idx + 1, meta["file"], record["reason"]))
            continue

        wav_path = meta["file"]
        if not os.path.isabs(wav_path):
            wav_path = os.path.join(base_dir, wav_path)
        samples, err = read_clip(wav_path)
        if err is not None:
            record["reason"] = err
            clip_records.append(record)
            print("[跳过] 片段%d %s —— %s" % (idx + 1, meta["file"], err))
            continue

        record["status"] = STATUS_OK
        record["frames"] = len(samples)
        ready.append((meta, samples))
        clip_records.append(record)
        print("[成功] 片段%d %s（%d 样本，%.3f 秒）"
              % (idx + 1, meta["file"], len(samples),
                 len(samples) / TARGET_RATE))

    # ---- 阶段 B：全部跳过 -> 只写报告，不写 WAV ----
    if not ready:
        for record in clip_records:
            if record["status"] == STATUS_SKIP and not record["reason"]:
                record["reason"] = "无可用片段"
        report = build_report(clip_records, None, no_usable=True)
        write_report(args.report_path, report)
        print("[警告] 所有片段均被跳过，未生成合成 WAV；无可用片段，"
              "各段原因见报告")
        print("报告输出路径: %s" % os.path.abspath(args.report_path))
        return 0

    # ---- 阶段 C：混音、交叉淡化、写盘 ----
    mix_buf, fade_records = mix(ready)
    ok_iter = iter(fade_records)
    for record in clip_records:
        if record["status"] == STATUS_OK:
            info = next(ok_iter)
            record["applied_fade_seconds"] = info["applied_fade_seconds"]
            record["note"] = info["note"]
            if info["note"]:
                print("[警告] 片段%d %s —— %s"
                      % (record["index"], record["file"], info["note"]))

    total_frames = write_wav(args.wav_path, mix_buf)
    report = build_report(clip_records, total_frames, no_usable=False)
    write_report(args.report_path, report)

    print("WAV 输出路径: %s" % os.path.abspath(args.wav_path))
    print("报告输出路径: %s" % os.path.abspath(args.report_path))
    return 0


if __name__ == "__main__":
    # Windows 控制台默认 GBK，强制 UTF-8 输出保证中文/警告行可识别
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    sys.exit(main())
