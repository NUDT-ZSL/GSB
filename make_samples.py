# -*- coding: utf-8 -*-
"""
确定性生成测试用 WAV 样本与编辑清单（仅标准库，无外部音频附件）。

生成内容（默认输出到 .\\work\\samples 与 .\\work\\manifests）：
  good_2s_22050.wav   2.0s 22050Hz 16位单声道 正弦波（440Hz）
  good_1s_44100.wav   1.0s 44100Hz 16位单声道 正弦波（880Hz, 幅度较低）
  good_3s_44100.wav   3.0s 44100Hz 16位单声道 正弦波（300Hz）
  stereo_2s.wav       2.0s 44100Hz 16位立体声（格式不符样本）
  pcm8_2s.wav         2.0s 44100Hz  8位单声道（格式不符样本）
  float_2s.wav        2.0s 44100Hz IEEE float（非 PCM 样本, 自写 WAVE_FORMAT_IEEE_FLOAT 头）
  missing.wav         不生成，由清单引用（缺失文件样本）

清单：
  edit_list.json      覆盖成功/越界截断/跳过/缺失/非PCM/位深不符/声道不符
  gap_list.json       两个 0.5s 片段 + 200ms 间隔（验收点 5）

用法:
  python make_samples.py [--workdir work]
"""

import argparse
import array
import json
import math
import os
import struct
import sys
import wave

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")


def sine_pcm16(path, duration, rate, freq, amplitude=0.5, phase=0.0):
    """生成确定性正弦波 16 位单声道 PCM WAV。"""
    n = int(round(duration * rate))
    samples = array.array("h")
    for i in range(n):
        v = amplitude * math.sin(2 * math.pi * freq * i / rate + phase)
        samples.append(int(round(v * 32767)))
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(samples.tobytes())


def stereo_pcm16(path, duration, rate, freq=440.0):
    n = int(round(duration * rate))
    samples = array.array("h")
    for i in range(n):
        v = int(round(0.4 * math.sin(2 * math.pi * freq * i / rate) * 32767))
        samples.append(v)                      # 左声道
        samples.append(-v)                     # 右声道
    with wave.open(path, "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(samples.tobytes())


def pcm8_mono(path, duration, rate, freq=440.0):
    n = int(round(duration * rate))
    samples = bytearray()
    for i in range(n):
        v = 0.5 + 0.4 * math.sin(2 * math.pi * freq * i / rate)  # 0..1 无符号
        samples.append(int(round(v * 255)))
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(1)
        wf.setframerate(rate)
        wf.writeframes(bytes(samples))


def ieee_float_wav(path, duration, rate, freq=440.0):
    """手写 WAVE_FORMAT_IEEE_FLOAT(3) 的 WAV，wave 模块无法打开 -> 非 PCM 样本。"""
    n = int(round(duration * rate))
    data = b"".join(
        struct.pack("<f", 0.5 * math.sin(2 * math.pi * freq * i / rate))
        for i in range(n)
    )
    byte_rate = rate * 4
    with open(path, "wb") as f:
        f.write(b"RIFF")
        f.write(struct.pack("<I", 36 + len(data)))
        f.write(b"WAVE")
        f.write(b"fmt ")
        f.write(struct.pack("<IHHIIHH", 16, 3, 1, rate,
                            byte_rate, 4, 32))  # format=3 float, 32bit
        f.write(b"data")
        f.write(struct.pack("<I", len(data)))
        f.write(data)


def write_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def main():
    parser = argparse.ArgumentParser(description="生成确定性 WAV 测试样本与清单")
    parser.add_argument("--workdir", default="work", help="工作目录（默认 work）")
    args = parser.parse_args()

    work = os.path.abspath(args.workdir)
    sample_dir = os.path.join(work, "samples")
    output_dir = os.path.join(work, "output")
    os.makedirs(sample_dir, exist_ok=True)
    os.makedirs(output_dir, exist_ok=True)

    sine_pcm16(os.path.join(sample_dir, "good_2s_22050.wav"),
               2.0, 22050, 440.0, amplitude=0.5)
    sine_pcm16(os.path.join(sample_dir, "good_1s_44100.wav"),
               1.0, 44100, 880.0, amplitude=0.2)
    sine_pcm16(os.path.join(sample_dir, "good_3s_44100.wav"),
               3.0, 44100, 300.0, amplitude=0.6)
    stereo_pcm16(os.path.join(sample_dir, "stereo_2s.wav"), 2.0, 44100)
    pcm8_mono(os.path.join(sample_dir, "pcm8_2s.wav"), 2.0, 44100)
    ieee_float_wav(os.path.join(sample_dir, "float_2s.wav"), 2.0, 44100)

    # 主清单：覆盖全部验收分支（路径相对清单文件所在目录的上两级 -> samples）
    edit_list = {
        "target_sample_rate": 44100,
        "segments": [
            {  # 成功：22050Hz 输入，整段，目标 -18 dBFS
                "file": "good_2s_22050.wav",
                "start_ms": 0, "end_ms": 2000,
                "target_dbfs": -18.0, "silence_after_ms": 100
            },
            {  # 成功：越界截断，2s 素材请求 1500-3000ms -> 0.5s（同时覆盖重采样）
                "file": "good_2s_22050.wav",
                "start_ms": 1500, "end_ms": 3000,
                "target_dbfs": -20.0, "silence_after_ms": 100
            },
            {  # 跳过：起始大于结束
                "file": "good_1s_44100.wav",
                "start_ms": 800, "end_ms": 200,
                "target_dbfs": -18.0, "silence_after_ms": 100
            },
            {  # 错误：文件缺失
                "file": "missing.wav",
                "start_ms": 0, "end_ms": 500,
                "target_dbfs": -18.0, "silence_after_ms": 100
            },
            {  # 错误：非 PCM（IEEE float）
                "file": "float_2s.wav",
                "start_ms": 0, "end_ms": 500,
                "target_dbfs": -18.0, "silence_after_ms": 100
            },
            {  # 错误：8 位位深不符
                "file": "pcm8_2s.wav",
                "start_ms": 0, "end_ms": 500,
                "target_dbfs": -18.0, "silence_after_ms": 100
            },
            {  # 错误：立体声不符
                "file": "stereo_2s.wav",
                "start_ms": 0, "end_ms": 500,
                "target_dbfs": -18.0, "silence_after_ms": 100
            },
            {  # 成功：跳过/错误片段不影响后续条目
                "file": "good_1s_44100.wav",
                "start_ms": 0, "end_ms": 500,
                "target_dbfs": -16.0, "silence_after_ms": 0
            }
        ]
    }

    # 间隔清单：两个 0.5s 片段 + 200ms 静音 -> 成功片段总长 1.2s
    gap_list = {
        "target_sample_rate": 44100,
        "segments": [
            {
                "file": "good_2s_22050.wav",
                "start_ms": 0, "end_ms": 500,
                "target_dbfs": -18.0, "silence_after_ms": 200
            },
            {
                "file": "good_1s_44100.wav",
                "start_ms": 0, "end_ms": 500,
                "target_dbfs": -20.0, "silence_after_ms": 0
            }
        ]
    }

    # 清单与样本同目录，process.py 以清单所在目录解析相对 file 路径
    write_json(os.path.join(sample_dir, "edit_list.json"), edit_list)
    write_json(os.path.join(sample_dir, "gap_list.json"), gap_list)

    print("样本与清单已生成:")
    for root, _dirs, files in os.walk(work):
        for name in sorted(files):
            p = os.path.join(root, name)
            print("  %s  (%d bytes)" % (
                os.path.relpath(p, work), os.path.getsize(p)))


if __name__ == "__main__":
    main()
