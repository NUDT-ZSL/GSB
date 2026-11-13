#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
make_samples.py —— 确定性生成小规模 16bit PCM WAV 测试素材与标记文本

全部波形使用整数三角波公式生成（不依赖随机数与第三方库），
任何机器重复运行得到字节一致的样本。总大小约 1MB，远低于 10MB 限制。

生成内容（samples/ 目录）
-------------------------
trackA_44100_mono.wav   44100Hz 单声道：语音峰 + 5 秒静音 + 语音峰（测静音压缩）
trackB_22050_mono.wav   22050Hz 单声道：两个语音峰（测采样率统一）
stereo_44100.wav        44100Hz 立体声：左右声道不同波形（测取第一声道）
silence_44100_mono.wav  44100Hz 全零轨（测全静音轨）
notaudio.wav            纯文本改名的假 WAV（测格式拒绝）
lowpeak_44100_mono.wav  低峰值单语音段（测归一化）
clipA_44100.wav         同相正脉冲（测重叠限幅，配合 clipB 重叠放置）
clipB_44100.wav         与 clipA 完全相同，相加必然超 16bit 上限
markers_demo.txt        覆盖 A/B 各段的合法标记
markers_bad.txt         含负采样、颠倒区间、越界、非整数等非法行 + 合法行
markers_shuffled.txt    区间乱序但全部合法（测报告排序）
markers_nolabel.txt     仅三字段无标签（测 label=null）
"""
from __future__ import annotations

import os
import struct

SR_HI = 44100
SR_LO = 22050


def triangle(n: int, period: int, peak: int) -> list:
    """整数三角波，取值 [-peak, peak]，完全确定、跨平台一致。"""
    half = max(period // 2, 1)
    out = []
    for i in range(n):
        x = i % period
        if x < half:
            v = -peak + (2 * peak * x) // half
        else:
            v = peak - (2 * peak * (x - half)) // half
        out.append(v)
    return out


def zeros(n: int) -> list:
    return [0] * n


def write_wav_mono(path: str, samples: list, sample_rate: int) -> None:
    data = struct.pack("<%dh" % len(samples), *samples)
    _write_wav(path, data, sample_rate, channels=1)


def write_wav_stereo(path: str, left: list, right: list, sample_rate: int) -> None:
    assert len(left) == len(right)
    interleaved = [v for pair in zip(left, right) for v in pair]
    data = struct.pack("<%dh" % len(interleaved), *interleaved)
    _write_wav(path, data, sample_rate, channels=2)


def _write_wav(path: str, data: bytes, sample_rate: int, channels: int) -> None:
    block_align = channels * 2
    byte_rate = sample_rate * block_align
    riff = (
        b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE"
        + b"fmt " + struct.pack("<IHHIIHH", 16, 1, channels, sample_rate,
                                byte_rate, block_align, 16)
        + b"data" + struct.pack("<I", len(data)) + data
    )
    with open(path, "wb") as f:
        f.write(riff)


def main() -> None:
    here = os.path.dirname(os.path.abspath(__file__))
    out_dir = os.path.join(here, "samples")
    os.makedirs(out_dir, exist_ok=True)

    burst = SR_HI * 3 // 10  # 0.3 秒 = 13230，105 采样周期可整除，burst 边界干净归零
    five_silence = SR_HI * 5  # 220500 采样的长静音

    # 1) A 轨：低峰值语音峰(12000) + 5s 静音 + 更低语音峰(9000)
    a = (triangle(burst, 105, 12000) + zeros(five_silence)
         + triangle(burst, 105, 9000))
    write_wav_mono(os.path.join(out_dir, "trackA_44100_mono.wav"), a, SR_HI)

    # 2) B 轨（22050Hz）：0.4s 语音 + 0.5s 静音 + 0.4s 语音 + 0.5s 尾部静音
    #    重采样到 44100 后长度精确翻倍（比例 2:1）；周期取整数保证边界归零
    b_burst = SR_LO * 4 // 10   # 8820  → 重采样后 17640 (0.4s)
    b_gap = SR_LO // 2          # 11025 → 重采样后 22050 (0.5s)，压缩为 13230
    b_tail = SR_LO // 2         # 11025 → 重采样后 22050，尾部静音被裁掉
    b = (triangle(b_burst, 21, 15000) + zeros(b_gap)
         + triangle(b_burst, 21, 11000) + zeros(b_tail))
    write_wav_mono(os.path.join(out_dir, "trackB_22050_mono.wav"), b, SR_LO)

    # 3) 立体声：左声道周期 147(约 300Hz) 峰值 20000，右声道周期 49(约 900Hz) 峰值 4000
    n_st = SR_HI // 2  # 0.5 秒 = 22050，可被 147/49 整除，边界归零
    left = triangle(n_st, 147, 20000)
    right = triangle(n_st, 49, 4000)
    write_wav_stereo(os.path.join(out_dir, "stereo_44100.wav"), left, right, SR_HI)

    # 4) 全静音轨
    write_wav_mono(os.path.join(out_dir, "silence_44100_mono.wav"),
                   zeros(SR_HI // 2), SR_HI)

    # 5) 假 WAV：纯文本
    with open(os.path.join(out_dir, "notaudio.wav"), "w", encoding="utf-8") as f:
        f.write("这不是一个 WAV 文件，只是被改了扩展名的纯文本。\n")

    # 6) 低峰值语音段（峰值仅 800，高于静音阈值 500；长度 11025 可被周期 105 整除）
    write_wav_mono(os.path.join(out_dir, "lowpeak_44100_mono.wav"),
                   triangle(SR_HI // 4, 105, 800), SR_HI)

    # 7) 限幅测试对：完全相同的同相正脉冲（含零间隔，无负值翻转风险）
    clip_wave = triangle(SR_HI // 10, 49, 20000)  # 0.1s = 4410，可被 49 整除
    write_wav_mono(os.path.join(out_dir, "clipA_44100.wav"), clip_wave, SR_HI)
    write_wav_mono(os.path.join(out_dir, "clipB_44100.wav"), list(clip_wave), SR_HI)

    # ---- 标记文件（坐标均为成品 44100Hz 时间线，数值由固定样本布局推导）----
    # A 处理后: 语音 0..13230, 停顿 13230..26460, 语音 26460..39690
    # B 紧接其后: 语音 39690..57330, 停顿 57330..70560, 语音 70560..88200
    with open(os.path.join(out_dir, "markers_demo.txt"), "w", encoding="utf-8") as f:
        f.write("# 演示标记：起始 结束 来源轨 标签\n")
        f.write("0 13230 A 开场白\n")
        f.write("26460 39690 A 第二段发言\n")
        f.write("39690 57330 B B主播发言\n")
        f.write("70560 88200 B B主播总结\n")

    with open(os.path.join(out_dir, "markers_bad.txt"), "w", encoding="utf-8") as f:
        f.write("# 含多种非法行，合法行必须仍然生效\n")
        f.write("-100 200 A 负起始采样行\n")
        f.write("0 13230 A 合法开场\n")
        f.write("500 300 A 起止颠倒行\n")
        f.write("0 999999 B 越界行\n")
        f.write("26460 39690 A 合法第二句\n")
        f.write("abc 100 B 非整数行\n")
        f.write("100 200 C 非法来源轨\n")

    with open(os.path.join(out_dir, "markers_shuffled.txt"), "w", encoding="utf-8") as f:
        f.write("# 区间故意乱序，报告 segments 仍须升序\n")
        f.write("70560 88200 B 最后一句\n")
        f.write("0 13230 A 第一句\n")
        f.write("39690 57330 B 中间句\n")

    with open(os.path.join(out_dir, "markers_nolabel.txt"), "w", encoding="utf-8") as f:
        f.write("# 只有三个字段，标签应为 null\n")
        f.write("0 13230 A\n")
        f.write("26460 39690 A\n")

    total = 0
    for name in sorted(os.listdir(out_dir)):
        size = os.path.getsize(os.path.join(out_dir, name))
        total += size
        print(f"{name:28s} {size:>8d} bytes")
    print(f"合计 {total} bytes ({total / 1024 / 1024:.2f} MB)，输出目录: {out_dir}")


if __name__ == "__main__":
    main()
