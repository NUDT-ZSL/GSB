#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""播客对白后期剪辑工具。

读取单声道 16 位 PCM WAV 与 JSON 剪辑脚本，删除脚本指定的无效段落，
输出精简 WAV 与固定字段的 JSON 审核报告。

用法:
    python trim_audio.py 原始.wav 剪辑脚本.json 输出.wav 报告.json [--force]

剪辑脚本格式:
    {"sample_rate": 16000, "intervals": [[起始秒, 结束秒], ...]}

报告固定字段:
    output_wav, original_frames, output_frames, applied, rejected, complete
"""
from __future__ import annotations

import json
import os
import sys
import wave
from decimal import Decimal, InvalidOperation

MILLI = Decimal("0.001")


class TrimError(Exception):
    """致命错误：不生成任何输出，非零退出。"""


def load_script(path):
    """读取并做顶层结构校验，返回 (sample_rate, intervals)。"""
    if not os.path.isfile(path):
        raise TrimError(f"剪辑脚本不存在: {path}")
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        raise TrimError(f"无法读取剪辑脚本 {path}: {e}")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise TrimError(f"剪辑脚本 JSON 语法错误: {e}")
    if not isinstance(data, dict):
        raise TrimError("剪辑脚本顶层必须是 JSON 对象")
    rate = data.get("sample_rate")
    if not isinstance(rate, int) or isinstance(rate, bool):
        raise TrimError("剪辑脚本缺少整数类型的 sample_rate 字段")
    intervals = data.get("intervals")
    if not isinstance(intervals, list):
        raise TrimError("剪辑脚本缺少数组类型的 intervals 字段")
    return rate, intervals


def load_wav(path):
    """打开原始 WAV 并校验为 16 位单声道 PCM，返回 (采样率, 总帧数, PCM字节)。"""
    try:
        w = wave.open(path, "rb")
    except (wave.Error, OSError, EOFError) as e:
        raise TrimError(f"无法打开原始 WAV {path}: {e}")
    with w:
        if w.getnchannels() != 1:
            raise TrimError(f"拒绝处理: 声道数为 {w.getnchannels()}，仅支持单声道")
        if w.getsampwidth() != 2:
            raise TrimError(
                f"拒绝处理: 位深为 {w.getsampwidth() * 8} 位，仅支持 16 位 PCM"
            )
        if w.getcomptype() != "NONE":
            raise TrimError(f"拒绝处理: 压缩类型 {w.getcomptype()}，仅支持无压缩 PCM")
        rate = w.getframerate()
        total = w.getnframes()
        pcm = w.readframes(total)
    return rate, total, pcm


def parse_seconds(value):
    """校验为最多三位小数的秒数，返回 Decimal；非法返回 None。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        d = Decimal(str(value))
        if not d.is_finite():
            return None
        if d != d.quantize(MILLI):
            return None
    except InvalidOperation:
        return None
    return d


def classify_intervals(intervals, rate, total_frames):
    """把脚本区间分为有效（帧区间）与被拒（附原因）两类。"""
    duration = Decimal(total_frames) / Decimal(rate)
    valid = []
    rejected = []
    for item in intervals:
        if not isinstance(item, list) or len(item) != 2:
            rejected.append(
                {"interval": item, "reason": "区间必须恰好包含起始与结束两个元素"}
            )
            continue
        s = parse_seconds(item[0])
        e = parse_seconds(item[1])
        if s is None or e is None:
            rejected.append(
                {"interval": item, "reason": "起止秒数必须为最多三位小数的数值"}
            )
            continue
        if s >= e:
            rejected.append(
                {"interval": item, "reason": "起始秒数必须小于结束秒数"}
            )
            continue
        if s < 0 or e > duration:
            rejected.append(
                {
                    "interval": item,
                    "reason": f"区间越界: 有效范围为 [0, {duration}] 秒",
                }
            )
            continue
        # 秒数最多三位小数，乘采样率必为整数帧，边界与总帧数一致
        valid.append((int(s * rate), int(e * rate)))
    return valid, rejected


def merge_intervals(valid):
    """合并重叠或相邻的帧区间，按起始升序返回。"""
    merged = []
    for s, e in sorted(valid):
        if merged and s <= merged[-1][1]:
            if e > merged[-1][1]:
                merged[-1][1] = e
        else:
            merged.append([s, e])
    return merged


def build_output(pcm, merged):
    """按合并后的删减区间拼接保留段，帧粒度（2 字节）对齐。"""
    out = bytearray()
    pos = 0
    for s, e in merged:
        out += pcm[pos * 2 : s * 2]
        pos = e
    out += pcm[pos * 2 :]
    return bytes(out)


def frames_to_seconds(frames, rate):
    return float(Decimal(frames) / Decimal(rate))


def main(argv):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    args = [a for a in argv[1:] if a != "--force"]
    force = len(args) != len(argv) - 1
    if len(args) != 4:
        sys.stderr.write(__doc__)
        return 2
    wav_in, script_in, wav_out, report_out = args
    try:
        if os.path.abspath(wav_in) == os.path.abspath(wav_out):
            raise TrimError("输出路径不能与原始 WAV 相同")
        rate, total_frames, pcm = load_wav(wav_in)
        script_rate, intervals = load_script(script_in)
        if script_rate != rate:
            raise TrimError(
                f"采样率不一致: 脚本为 {script_rate} Hz，音频为 {rate} Hz，拒绝执行"
            )
        if not force:
            for p in (wav_out, report_out):
                if os.path.exists(p):
                    raise TrimError(
                        f"输出路径已存在，拒绝覆盖: {p}（如需覆盖请使用 --force）"
                    )
        valid, rejected = classify_intervals(intervals, rate, total_frames)
        merged = merge_intervals(valid)
        out_pcm = build_output(pcm, merged)
        out_frames = total_frames - sum(e - s for s, e in merged)

        # 先写 WAV
        try:
            with wave.open(wav_out, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(rate)
                w.writeframes(out_pcm)
        except OSError as e:
            raise TrimError(f"写入输出 WAV 失败: {e}")

        report = {
            "output_wav": wav_out,
            "original_frames": total_frames,
            "output_frames": out_frames,
            "applied": [
                [frames_to_seconds(s, rate), frames_to_seconds(e, rate)]
                for s, e in merged
            ],
            "rejected": rejected,
            "complete": len(rejected) == 0,
        }
        # 再写报告（临时文件 + 替换）；失败则清理已写出的 WAV，不残留
        tmp = report_out + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(report, f, ensure_ascii=False, indent=2)
            os.replace(tmp, report_out)
        except OSError as e:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            try:
                os.unlink(wav_out)
            except OSError:
                pass
            raise TrimError(f"写入审核报告失败: {e}（已清理输出 WAV）")

        print(
            f"摘要: 输出帧数={out_frames} "
            f"生效区间数={len(merged)} 被拒区间数={len(rejected)}"
        )
        return 0
    except TrimError as e:
        print(f"错误: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
