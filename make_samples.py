#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
make_samples.py —— 生成本地确定性的 16 位 PCM WAV 测试样本与全部编辑清单。

仅使用标准库。运行：
    python -I make_samples.py [样本目录=sample_data]

生成的 WAV（均为确定性正弦/常量信号，无随机数）：
  a_44100_mono_300ms.wav    44100Hz 单声道 300ms 440Hz 半幅正弦
  b_22050_mono_300ms.wav    22050Hz 单声道 300ms 880Hz 半幅正弦
  c_44100_stereo_350ms.wav  44100Hz 双声道 350ms 满幅正弦（左右反相）
  d_22050_stereo_350ms.wav  22050Hz 双声道 350ms 满幅正弦
  e_8bit_44100.wav          44100Hz 单声道 8 位 PCM（用于非法格式验收）
"""

import array
import json
import math
import os
import struct
import sys
import wave


def sine_frames(duration_ms, rate, freq, amplitude, channels=1,
                phase_invert_right=False):
    """生成确定性正弦波的 16 位交错采样列表。"""
    n = int(round(duration_ms * rate / 1000.0))
    frames = []
    for i in range(n):
        v = int(round(amplitude * math.sin(2.0 * math.pi * freq * i / rate)))
        if channels == 1:
            frames.append(v)
        else:
            frames.append(v)
            frames.append(-v if phase_invert_right else v)
    return frames


def write_wav_16(path, rate, channels, samples):
    with wave.open(path, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.setcomptype("NONE", "not compressed")
        data = array.array("h", samples)
        if sys.byteorder != "little":
            data.byteswap()
        wf.writeframes(data.tobytes())


def write_wav_8bit(path, rate, samples_u8):
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(1)  # 8 位 PCM，用于非法输入验收
        wf.setframerate(rate)
        wf.setcomptype("NONE", "not compressed")
        wf.writeframes(struct.pack("<%dB" % len(samples_u8), *samples_u8))


def build_manifests():
    """返回 {清单文件名: 清单对象}，覆盖任务的 10 条验收场景。"""
    A = "../wav/a_44100_mono_300ms.wav"
    B = "../wav/b_22050_mono_300ms.wav"
    C = "../wav/c_44100_stereo_350ms.wav"
    D = "../wav/d_22050_stereo_350ms.wav"
    MISSING = "../wav/not_exist_clip.wav"
    BAD8 = "../wav/e_8bit_44100.wav"

    out = {"wav": "output.wav", "markers": "markers.json",
           "report": "report.json"}

    manifests = {}

    # T1/T2：两片段 + 一标记；首片段截取 200ms，次片段无起始（首尾相接）
    manifests["manifest_01_basic.json"] = {
        "output": out,
        "clips": [
            {"path": A, "start_ms": 0, "duration_ms": 200},
            {"path": B},
        ],
        "markers": [
            {"name": "mark-middle", "time_ms": 100},
        ],
    }

    # T3：不同采样率，统一到首片段 44100Hz
    manifests["manifest_02_resample.json"] = {
        "output": out,
        "clips": [
            {"path": A, "start_ms": 0},
            {"path": B, "start_ms": 300},
        ],
        "markers": [
            {"name": "at-441-frame", "time_ms": 10},
        ],
    }

    # T4：两个满幅片段重叠 50ms（300ms + 300ms 起点 250ms => 总长 550ms）
    manifests["manifest_03_overlap.json"] = {
        "output": out,
        "clips": [
            {"path": C, "start_ms": 0},
            {"path": D, "start_ms": 250},
        ],
        "markers": [],
    }

    # T5：空片段 + 一个标记 => 0 帧、标记全部越界
    manifests["manifest_04_empty.json"] = {
        "output": out,
        "clips": [],
        "markers": [
            {"name": "lonely-mark", "time_ms": 0},
        ],
    }

    # T6：总时长 500ms，负时间 + 越界时间各一条，另含一条有效标记
    manifests["manifest_05_marker_bounds.json"] = {
        "output": out,
        "clips": [
            {"path": A, "start_ms": 0, "duration_ms": 250},
            {"path": B, "start_ms": 250, "duration_ms": 250},
        ],
        "markers": [
            {"name": "negative", "time_ms": -5},
            {"name": "over-end", "time_ms": 600},
            {"name": "valid", "time_ms": 250},
        ],
    }

    # T7a：引用不存在的文件
    manifests["manifest_06_missing_file.json"] = {
        "output": out,
        "clips": [
            {"path": MISSING, "start_ms": 0},
        ],
        "markers": [],
    }

    # T7b：引用 8 位（非 16 位 PCM）WAV
    manifests["manifest_07_bad_format.json"] = {
        "output": out,
        "clips": [
            {"path": BAD8, "start_ms": 0},
        ],
        "markers": [],
    }

    # T8a：增益 +20dB 超范围（上限 +12dB），整单拒绝
    manifests["manifest_08_gain_invalid.json"] = {
        "output": out,
        "clips": [
            {"path": A, "start_ms": 0, "gain_db": 20},
        ],
        "markers": [],
    }

    # T8b：增益 +6dB，半幅信号放大后仍限幅且可读
    manifests["manifest_09_gain_ok.json"] = {
        "output": out,
        "clips": [
            {"path": A, "start_ms": 0, "gain_db": 6},
        ],
        "markers": [],
    }

    # T9：与基础清单相同内容，用于覆盖同名旧文件 + 报告一致性
    manifests["manifest_10_overwrite.json"] = {
        "output": out,
        "clips": [
            {"path": A, "start_ms": 0, "duration_ms": 200},
            {"path": B},
        ],
        "markers": [
            {"name": "mark-middle", "time_ms": 100},
        ],
    }

    # T10：起始 -10ms 与增益为文本，两个字段错误同时出现
    manifests["manifest_11_field_errors.json"] = {
        "output": out,
        "clips": [
            {"path": A, "start_ms": -10, "duration_ms": 100,
             "gain_db": "6dB"},
        ],
        "markers": [],
    }

    return manifests


def main(argv=None):
    base = argv[1] if argv and len(argv) > 1 else "sample_data"
    wav_dir = os.path.join(base, "wav")
    manifest_dir = os.path.join(base, "manifests")
    os.makedirs(wav_dir, exist_ok=True)
    os.makedirs(manifest_dir, exist_ok=True)

    HALF = 32767 // 2
    FULL = 32767

    write_wav_16(os.path.join(wav_dir, "a_44100_mono_300ms.wav"),
                 44100, 1, sine_frames(300, 44100, 440, HALF))
    write_wav_16(os.path.join(wav_dir, "b_22050_mono_300ms.wav"),
                 22050, 1, sine_frames(300, 22050, 880, HALF))
    write_wav_16(os.path.join(wav_dir, "c_44100_stereo_350ms.wav"),
                 44100, 2,
                 sine_frames(350, 44100, 440, FULL, channels=2,
                             phase_invert_right=True))
    write_wav_16(os.path.join(wav_dir, "d_22050_stereo_350ms.wav"),
                 22050, 2,
                 sine_frames(350, 22050, 440, FULL, channels=2))
    write_wav_8bit(os.path.join(wav_dir, "e_8bit_44100.wav"), 44100,
                   [128 + int(60 * math.sin(2 * math.pi * 440 * i / 44100))
                    for i in range(int(0.1 * 44100))])

    for name, obj in build_manifests().items():
        with open(os.path.join(manifest_dir, name), "w",
                  encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)

    print("样本与清单已生成于: %s" % os.path.abspath(base))
    print("  WAV 目录 : %s" % wav_dir)
    print("  清单目录 : %s" % manifest_dir)


if __name__ == "__main__":
    main(sys.argv)
