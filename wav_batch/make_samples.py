# -*- coding: utf-8 -*-
"""确定性生成演示用 WAV 样本与 CSV 清单（仅标准库，无随机数）。

生成内容：
  samples/src/line_a_44100.wav   44100Hz 双声道 16位，时长 5.000s，约 -13.1 dBFS
  samples/src/line_b_22050.wav   22050Hz 双声道 16位，时长 4.000s，约 -0.26 dBFS（近满幅）
  samples/src/line_c_8000.wav     8000Hz 双声道 16位，时长 3.000s，约 -18 dBFS
  samples/src/mono8_11025.wav    11025Hz 单声道 8 位（格式不支持样本）
  samples/manifest.csv           覆盖成功/越界/缺失/格式错误/削波等场景
  samples/manifest_empty.csv     仅含表头的空清单（全局失败验收）
  samples/manifest_same_path.csv 输出路径与源相同的专用清单（同路径拒绝验收）
"""
from __future__ import annotations

import math
import os
import struct
import wave

SAMPLE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "samples")
SRC_DIR = os.path.join(SAMPLE_DIR, "src")


def _write_stereo16(path: str, sample_rate: int, duration: float,
                    left_fn, right_fn) -> None:
    frames = int(round(sample_rate * duration))
    samples = []
    for n in range(frames):
        t = n / sample_rate
        samples.append(max(-32768, min(32767, int(round(left_fn(t) * 32767)))))
        samples.append(max(-32768, min(32767, int(round(right_fn(t) * 32767)))))
    with wave.open(path, "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(struct.pack("<%dh" % len(samples), *samples))


def _write_mono8(path: str, sample_rate: int, duration: float) -> None:
    """故意写成单声道 8 位 PCM，用于格式不支持验收。"""
    frames = int(round(sample_rate * duration))
    data = bytearray()
    for n in range(frames):
        t = n / sample_rate
        # 8 位 PCM 为无符号偏移编码，静音为 128
        v = int(round(128 + 60 * math.sin(2 * math.pi * 220 * t)))
        data.append(max(0, min(255, v)))
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(1)
        wf.setframerate(sample_rate)
        wf.writeframes(bytes(data))


def main() -> None:
    os.makedirs(SRC_DIR, exist_ok=True)

    # 1) 44100Hz / 5s：220Hz 正弦，幅度 0.30，RMS≈0.2121 → 约 -13.1 dBFS
    _write_stereo16(
        os.path.join(SRC_DIR, "line_a_44100.wav"), 44100, 5.0,
        left_fn=lambda t: 0.30 * math.sin(2 * math.pi * 220 * t),
        right_fn=lambda t: 0.30 * math.sin(2 * math.pi * 220 * t + 0.5),
    )

    # 2) 22050Hz / 4s：440Hz 正弦，幅度 0.97（近满幅，目标 0dBFS 时触发削波回退）
    _write_stereo16(
        os.path.join(SRC_DIR, "line_b_22050.wav"), 22050, 4.0,
        left_fn=lambda t: 0.97 * math.sin(2 * math.pi * 440 * t),
        right_fn=lambda t: 0.97 * math.sin(2 * math.pi * 440 * t + 0.25),
    )

    # 3) 8000Hz / 3s：300Hz 正弦，幅度 0.25，RMS≈0.1768 → 约 -18 dBFS
    _write_stereo16(
        os.path.join(SRC_DIR, "line_c_8000.wav"), 8000, 3.0,
        left_fn=lambda t: 0.25 * math.sin(2 * math.pi * 300 * t),
        right_fn=lambda t: 0.25 * math.sin(2 * math.pi * 300 * t),
    )

    # 4) 单声道 8 位：格式不支持
    _write_mono8(os.path.join(SRC_DIR, "mono8_11025.wav"), 11025, 2.0)

    # 清单行刻意覆盖全部验收异常；行顺序即报告顺序
    manifest_rows = [
        # id, source, start, end, target_dbfs, output
        ("A_normal_44100", "src/line_a_44100.wav", "1.234", "2.345", "-20", "A_normal.wav"),
        ("B_clip_guard", "src/line_b_22050.wav", "0.500", "1.500", "0", "B_clip_guard.wav"),
        ("C_keep_8000", "src/line_c_8000.wav", "0.000", "1.000", "-20", "C_keep_8000.wav"),
        ("D_missing_file", "src/not_exist.wav", "0.000", "1.000", "-20", "D_missing.wav"),
        ("E_bad_format", "src/mono8_11025.wav", "0.000", "1.000", "-20", "E_bad_format.wav"),
        ("F_time_overflow", "src/line_a_44100.wav", "1.000", "10.000", "-20", "F_overflow.wav"),
        ("G_bad_interval", "src/line_a_44100.wav", "2.000", "2.000", "-20", "G_bad_interval.wav"),
        ("H_target_range", "src/line_a_44100.wav", "0.000", "1.000", "-45", "H_target.wav"),
        ("J_duration_check", "src/line_a_44100.wav", "1.000", "2.500", "-20", "J_duration.wav"),
    ]
    header = "id,source,start,end,target_dbfs,output\n"
    with open(os.path.join(SAMPLE_DIR, "manifest.csv"), "w", encoding="utf-8", newline="") as f:
        f.write(header)
        for r in manifest_rows:
            f.write(",".join(r) + "\n")

    with open(os.path.join(SAMPLE_DIR, "manifest_empty.csv"), "w", encoding="utf-8", newline="") as f:
        f.write(header)

    # 同路径专用清单：运行时输出目录指定为 samples，
    # 则 source 与 output 都解析到 samples/src/line_a_44100.wav
    with open(os.path.join(SAMPLE_DIR, "manifest_same_path.csv"), "w", encoding="utf-8", newline="") as f:
        f.write(header)
        f.write("K_same_path,src/line_a_44100.wav,0.000,1.000,-20,src/line_a_44100.wav\n")

    print("样本已生成于：%s" % SAMPLE_DIR)
    for name in sorted(os.listdir(SRC_DIR)):
        print("  src/%s" % name)
    print("  manifest.csv（%d 条数据行）" % len(manifest_rows))
    print("  manifest_empty.csv（仅表头）")
    print("  manifest_same_path.csv（输出与输入相同场景）")


if __name__ == "__main__":
    main()
