# -*- coding: utf-8 -*-
"""
确定性测试数据生成器。

在指定工作区下生成：
  wav/        —— 若干 16-bit PCM 单声道 WAV（正弦波，公式固定，可复现）
  *.json      —— 覆盖 11 项验收场景的描述文件

不依赖第三方库，仅使用标准库 math/struct/wave/json。
"""

import argparse
import json
import math
import os
import struct
import wave

SAMPLE_RATE = 48000
TARGET_DB = -20.0          # 描述文件统一基准响度
WAV_SUBDIR = "wav"


def write_sine_wav(path, duration_sec, sample_rate, amp, freq=440.0,
                   phase0=0.0, chunk_seconds=1.0):
    """
    生成确定性正弦波 WAV：x[n] = amp * sin(2*pi*freq*n/sr + phase0)。
    分块写入，避免 6 分钟样本一次性占用过大内存。
    """
    total = int(round(duration_sec * sample_rate))
    block = max(1, int(chunk_seconds * sample_rate))
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        n = 0
        while n < total:
            m = min(block, total - n)
            buf = bytearray(m * 2)
            for k in range(m):
                idx = n + k
                value = amp * math.sin(
                    2.0 * math.pi * freq * idx / sample_rate + phase0
                )
                value = max(-32768, min(32767, int(value)))
                struct.pack_into("<h", buf, k * 2, value)
            wf.writeframesraw(bytes(buf))
            n += m
    return total


def seg(wav_name, start, end):
    """描述文件片段条目；路径相对描述文件所在目录。"""
    return {"path": f"{WAV_SUBDIR}/{wav_name}", "start_sec": start, "end_sec": end}


def write_desc(workspace, name, segments, sample_rate=SAMPLE_RATE,
               target_db=TARGET_DB, output_file=None):
    desc = {
        "output_file": output_file or f"{name}_output.wav",
        "sample_rate": sample_rate,
        "target_loudness_db": target_db,
        "segments": segments,
    }
    path = os.path.join(workspace, f"{name}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(desc, f, ensure_ascii=False, indent=2)
    return path


def generate(workspace):
    os.makedirs(workspace, exist_ok=True)
    wav_dir = os.path.join(workspace, WAV_SUBDIR)

    # ---- 1) 基础 WAV 样本（48kHz / 16bit / mono，公式固定） ----
    # 满幅 0.5 的正弦，理论 RMS dBFS = 20*log10(0.5/sqrt(2)) ≈ -9.03 dB
    write_sine_wav(os.path.join(wav_dir, "seg_a.wav"), 3.0, SAMPLE_RATE, 16384,
                   freq=330.0, phase0=0.0)
    write_sine_wav(os.path.join(wav_dir, "seg_b.wav"), 4.0, SAMPLE_RATE, 8192,
                   freq=440.0, phase0=0.5)
    write_sine_wav(os.path.join(wav_dir, "seg_c.wav"), 2.5, SAMPLE_RATE, 24000,
                   freq=550.0, phase0=1.0)
    write_sine_wav(os.path.join(wav_dir, "seg_d.wav"), 5.0, SAMPLE_RATE, 12000,
                   freq=660.0, phase0=1.5)

    # 低 10dB 的安静片段：幅度 0.15625（=0.5/sqrt(10)），RMS 约 -19.16 dB，
    # 目标 -20 dB 附近，验收 2 另有一个专门低 10dB 的描述（见 quiet 场景）。
    write_sine_wav(os.path.join(wav_dir, "seg_quiet.wav"), 3.0, SAMPLE_RATE,
                   int(16384 / math.sqrt(10.0)), freq=220.0)

    # 44.1kHz 片段（用于采样率不匹配）
    write_sine_wav(os.path.join(wav_dir, "seg_44100.wav"), 2.0, 44100, 16384,
                   freq=440.0)

    # 6 分钟长片段（约 34.6MB 磁盘占用）
    write_sine_wav(os.path.join(wav_dir, "seg_long.wav"), 6 * 60.0, SAMPLE_RATE,
                   16384, freq=300.0, chunk_seconds=4.0)

    # ---- 2) 描述文件：覆盖全部 11 项验收 ----
    # 场景 1/11：全部有效、顺序拼接（区间时长 2+3+1.5+4 = 10.5s）
    write_desc(workspace, "desc_valid", [
        seg("seg_a.wav", 0.0, 2.0),
        seg("seg_b.wav", 0.5, 3.5),
        seg("seg_c.wav", 0.0, 1.5),
        seg("seg_d.wav", 1.0, 5.0),
    ])

    # 场景 2：片段比基准低 10dB（基准 -20，构造 RMS 约 -30dB 的片段，增益 +10dB）
    # 幅度 5192 -> RMS ≈ 3671 -> dBFS ≈ -19.03；改为幅度 1638：RMS≈1158 -> -29.03dB
    write_sine_wav(os.path.join(wav_dir, "seg_minus10.wav"), 3.0, SAMPLE_RATE,
                   1638, freq=440.0)
    write_desc(workspace, "desc_quiet", [
        seg("seg_minus10.wav", 0.0, 3.0),
    ])

    # 场景 3：缺失片段 + 有效片段
    write_desc(workspace, "desc_missing", [
        seg("seg_not_exist.wav", 0.0, 2.0),
        seg("seg_a.wav", 0.0, 2.0),
    ])

    # 场景 4：无效区间（负数起止、结束<=开始、超出时长）
    write_desc(workspace, "desc_invalid", [
        seg("seg_a.wav", -1.0, 2.0),       # 负开始
        seg("seg_b.wav", 2.0, 2.0),        # 结束 == 开始
        seg("seg_c.wav", 1.0, 0.5),        # 结束 < 开始
        seg("seg_d.wav", 0.0, 99.0),       # 超出文件时长
        seg("seg_a.wav", 0.0, 1.0),        # 有效，保证仍有产出
    ])

    # 场景 5：全部失败（无效区间 + 缺失 + 采样率不匹配）
    write_desc(workspace, "desc_all_fail", [
        seg("seg_a.wav", -1.0, -0.5),
        seg("seg_not_exist.wav", 0.0, 1.0),
        seg("seg_44100.wav", 0.0, 1.0),
    ])

    # 场景 7：基准响度非法（-80 与 +10）
    write_desc(workspace, "desc_bad_ref_low", [seg("seg_a.wav", 0.0, 1.0)],
               target_db=-80.0, output_file="bad_ref_low_output.wav")
    write_desc(workspace, "desc_bad_ref_high", [seg("seg_a.wav", 0.0, 1.0)],
               target_db=10.0, output_file="bad_ref_high_output.wav")

    # 场景 8：采样率不匹配（片段 44100Hz，描述指定 48000Hz）+ 一个有效片段
    write_desc(workspace, "desc_rate_mismatch", [
        seg("seg_44100.wav", 0.0, 1.0),
        seg("seg_a.wav", 0.0, 1.0),
    ])

    # 场景 9：6 分钟长片段
    write_desc(workspace, "desc_long", [
        seg("seg_long.wav", 0.0, 6 * 60.0),
    ])

    # 场景 10：覆盖已存在输出
    write_desc(workspace, "desc_overwrite", [
        seg("seg_a.wav", 0.0, 1.0),
    ], output_file="overwrite_output.wav")

    # 场景 11b：默认输出目录（省略 --output-dir，输出到当前工作目录）
    write_desc(workspace, "desc_default_dir", [
        seg("seg_a.wav", 0.0, 1.0),
    ], output_file="default_dir_output.wav")

    return workspace


def main():
    parser = argparse.ArgumentParser(description="生成确定性测试 WAV 与描述文件")
    parser.add_argument("workspace", help="测试工作区目录（将创建 wav/ 与 *.json）")
    args = parser.parse_args()
    generate(os.path.abspath(args.workspace))
    print(f"测试数据已生成于: {os.path.abspath(args.workspace)}")


if __name__ == "__main__":
    main()
