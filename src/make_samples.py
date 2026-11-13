# -*- coding: utf-8 -*-
"""确定性生成演示与验证所需的全部样本。

生成内容（固定公式、固定采样率，重复运行字节级一致）:
  input/program.wav        节目，8000 Hz、16-bit、单声道，20.0 s（160000 采样点）
  input/ad.wav             广告，8000 Hz、16-bit、单声道， 2.0 s（ 16000 采样点）
  input/short_program.wav  短节目，8000 Hz，3.0 s（用于空间不足验证）
  input/stereo_program.wav 立体声节目（非法输入验证）
  input/rate16k_ad.wav     16000 Hz 广告（采样率不一致验证）
  plans/plan_valid.json         正常计划（含 2 次重复、多插入点）
  plans/plan_invalid_items.json 含负值/越界/非整数次数的计划（无效项跳过）
  plans/plan_overflow.json      短节目 + 靠后插入点（空间不足）
  plans/plan_missing_field.json 缺必需字段（致命错误）
  plans/plan_bad_time.json      时间非数字（致命错误）
  output_keep/placeholder.txt   占位文件，验证失败/重试时不被破坏

波形公式（便于逐采样点核对）:
  节目: sample[i] = round(8000 * sin(2*pi*220*i/R))，限幅到 [-32768, 32767]
  广告: sample[i] = round(12000 * sin(2*pi*880*i/R)) + (i % 97 - 48)
"""
from __future__ import annotations

import argparse
import array
import json
import math
import os
import sys
import wave

# -I 隔离模式下 PYTHONIOENCODING 不生效，显式把标准输出切到 UTF-8
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

RATE = 8000


def _clamp16(value: int) -> int:
    return max(-32768, min(32767, value))


def program_wave(n: int, rate: int = RATE, amp: int = 8000, freq: float = 220.0) -> array.array:
    samples = array.array("h")
    for i in range(n):
        samples.append(_clamp16(round(amp * math.sin(2 * math.pi * freq * i / rate))))
    return samples


def ad_wave(n: int, rate: int = RATE) -> array.array:
    samples = array.array("h")
    for i in range(n):
        value = round(12000 * math.sin(2 * math.pi * 880 * i / rate)) + (i % 97 - 48)
        samples.append(_clamp16(value))
    return samples


def write_wav(path: str, samples: array.array, rate: int, channels: int = 1, sampwidth: int = 2) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sampwidth)
        wf.setframerate(rate)
        if channels == 1:
            wf.writeframes(samples.tobytes())
        else:
            # 立体声：两路都放同一波形，保证是合法的 2 通道 16-bit WAV
            frame = array.array("h")
            for value in samples:
                frame.extend([value, value])
            wf.writeframes(frame.tobytes())


def write_json(path: str, payload) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="确定性生成广告拼接工具的演示样本")
    parser.add_argument("--root", default="demo", help="样本根目录（默认 demo）")
    args = parser.parse_args()
    root = os.path.abspath(args.root)

    input_dir = os.path.join(root, "input")
    plans_dir = os.path.join(root, "plans")
    keep_dir = os.path.join(root, "output_keep")

    # --- WAV 样本 ---
    program = program_wave(20 * RATE)                 # 20 s 节目
    ad = ad_wave(2 * RATE)                            # 2 s 广告
    short_program = program_wave(3 * RATE, freq=330.0)  # 3 s 短节目

    write_wav(os.path.join(input_dir, "program.wav"), program, RATE)
    write_wav(os.path.join(input_dir, "ad.wav"), ad, RATE)
    write_wav(os.path.join(input_dir, "short_program.wav"), short_program, RATE)
    write_wav(os.path.join(input_dir, "stereo_program.wav"), program, RATE, channels=2)
    write_wav(os.path.join(input_dir, "rate16k_ad.wav"), ad, 16000)

    # --- 计划 JSON ---
    write_json(
        os.path.join(plans_dir, "plan_valid.json"),
        {
            "insertions": [
                {"time_sec": 2.0, "repeats": 1},
                {"time_sec": 8.5, "repeats": 2},
                {"time_sec": 15.0, "repeats": 1},
            ]
        },
    )

    write_json(
        os.path.join(plans_dir, "plan_invalid_items.json"),
        {
            "insertions": [
                {"time_sec": 1.0, "repeats": 1},          # 有效
                {"time_sec": -2.0, "repeats": 1},         # 负时间 -> 跳过
                {"time_sec": 5.0, "repeats": 2.5},        # 非整数次数 -> 跳过
                {"time_sec": 99.0, "repeats": 1},         # 越界 -> 跳过
                {"time_sec": 10.0, "repeats": 3},         # 有效
                {"time_sec": 4.0, "repeats": 0},          # 非正次数 -> 跳过
            ]
        },
    )

    # 短节目 3 s，广告 2 s；在 2.5 s 处要求 3 次，仅剩 0.5 s -> 一次都放不下
    write_json(
        os.path.join(plans_dir, "plan_overflow.json"),
        {"insertions": [{"time_sec": 0.5, "repeats": 1}, {"time_sec": 2.5, "repeats": 3}]},
    )

    write_json(
        os.path.join(plans_dir, "plan_missing_field.json"),
        {"insertions": [{"time_sec": 1.0}]},  # 缺 repeats
    )
    write_json(
        os.path.join(plans_dir, "plan_bad_time.json"),
        {"insertions": [{"time_sec": "三秒", "repeats": 1}]},
    )

    # --- 占位文件（验证不被覆盖）---
    os.makedirs(keep_dir, exist_ok=True)
    placeholder = os.path.join(keep_dir, "placeholder.txt")
    if not os.path.exists(placeholder):
        with open(placeholder, "w", encoding="utf-8") as fh:
            fh.write("此文件用于验证工具不会破坏输出目录中的已有文件。\n")

    print("样本生成完成，根目录:", root)
    for dirpath, _dirs, files in os.walk(root):
        for name in sorted(files):
            full = os.path.join(dirpath, name)
            print(f"  {os.path.relpath(full, root)}  ({os.path.getsize(full)} bytes)")


if __name__ == "__main__":
    main()
