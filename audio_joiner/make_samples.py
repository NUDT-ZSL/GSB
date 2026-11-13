# -*- coding: utf-8 -*-
"""
确定性测试样本与时间线场景生成器（纯标准库）

运行后在指定目录（默认 ./demo_workspace）生成：
  wav/                确定性正弦波 WAV（样本由本脚本公式构造，无外部素材）
    clip_a.wav ...    单声道 16-bit 44100Hz 合规片段
    stereo48k.wav     立体声 48kHz 不合规模板片段
  edit.json           默认场景：两段 1 秒片段、淡化 0.2 秒
  edit_*.json         覆盖各项验收点的场景

用法：
  python -I make_samples.py [目标目录]
"""

import json
import math
import os
import sys
import wave

RATE = 44100
AMP = 12000  # 取较低振幅，保证重叠叠加也不会被夹值，样本级断言可精确比对


def write_mono16(path, freqs, seconds, rate=RATE):
    """用正弦公式确定性生成单声道 16-bit PCM WAV。

    freqs: 该片段包含的频率列表（Hz），多频率等幅叠加。
    """
    n = int(round(seconds * rate))
    scale = AMP / len(freqs)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        frames = bytearray()
        for i in range(n):
            value = sum(scale * math.sin(2 * math.pi * f * i / rate)
                        for f in freqs)
            frames += int(round(value)).to_bytes(2, "little", signed=True)
        wf.writeframes(bytes(frames))


def write_stereo48(path, seconds=1.0, rate=48000):
    """生成 立体声 / 16-bit / 48kHz 的不合规模板片段。"""
    n = int(round(seconds * rate))
    with wave.open(path, "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        frames = bytearray()
        for i in range(n):
            left = int(round(AMP * 0.5 * math.sin(2 * math.pi * 440 * i / rate)))
            right = int(round(AMP * 0.5 * math.sin(2 * math.pi * 660 * i / rate)))
            frames += left.to_bytes(2, "little", signed=True)
            frames += right.to_bytes(2, "little", signed=True)
        wf.writeframes(bytes(frames))


def write_json(path, clips):
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"clips": clips}, f, ensure_ascii=False, indent=2)


def build(workspace):
    wav_dir = os.path.join(workspace, "wav")
    os.makedirs(wav_dir, exist_ok=True)

    # ---- 合规片段：不同频率组合，1 秒（clip_e 为 0.5 秒短片段）----
    write_mono16(os.path.join(wav_dir, "clip_a.wav"), [220.0], 1.0)
    write_mono16(os.path.join(wav_dir, "clip_b.wav"), [330.0], 1.0)
    write_mono16(os.path.join(wav_dir, "clip_c.wav"), [440.0], 1.0)
    write_mono16(os.path.join(wav_dir, "clip_d.wav"), [550.0], 1.0)
    write_mono16(os.path.join(wav_dir, "clip_e.wav"), [880.0], 0.5)
    write_stereo48(os.path.join(wav_dir, "stereo48k.wav"))

    def w(name):
        # JSON 内使用相对路径，由 joiner 相对 JSON 所在目录解析
        return "wav/" + name

    scenarios = {
        # 1. 默认场景：两段 1 秒、淡化 0.2 -> 输出 1.8 秒
        "edit.json": [
            {"file": w("clip_a.wav"), "start": 0.0, "fade": 0.0},
            {"file": w("clip_b.wav"), "start": 0.8, "fade": 0.2},
        ],
        # 4. 淡化 0.8 秒超出 0.2 秒重叠 -> 该处不淡化
        "edit_fade_too_big.json": [
            {"file": w("clip_a.wav"), "start": 0.0, "fade": 0.0},
            {"file": w("clip_b.wav"), "start": 0.8, "fade": 0.8},
        ],
        # 2. 中间夹一个 48kHz 立体声片段 -> 跳过，其余正常
        "edit_bad_format.json": [
            {"file": w("clip_a.wav"), "start": 0.0, "fade": 0.0},
            {"file": w("stereo48k.wav"), "start": 1.0, "fade": 0.0},
            {"file": w("clip_c.wav"), "start": 2.0, "fade": 0.0},
        ],
        # 3. 含不存在路径
        "edit_missing.json": [
            {"file": w("clip_a.wav"), "start": 0.0, "fade": 0.0},
            {"file": w("not_exist.wav"), "start": 1.0, "fade": 0.0},
            {"file": w("clip_b.wav"), "start": 2.0, "fade": 0.0},
        ],
        # 10. 中间片段缺失，剩余片段保持原时间线位置，不插静音
        "edit_middle_missing.json": [
            {"file": w("clip_a.wav"), "start": 0.0, "fade": 0.0},
            {"file": w("not_exist.wav"), "start": 1.0, "fade": 0.0},
            {"file": w("clip_b.wav"), "start": 2.0, "fade": 0.0},
        ],
        # 7. 全部缺失 -> 无 WAV，报告标明无可用片段
        "edit_all_missing.json": [
            {"file": w("nope1.wav"), "start": 0.0, "fade": 0.0},
            {"file": w("nope2.wav"), "start": 1.0, "fade": 0.0},
        ],
        # 6. 非严格递增 -> 整体拒绝
        "edit_bad_timeline.json": [
            {"file": w("clip_a.wav"), "start": 1.0, "fade": 0.0},
            {"file": w("clip_b.wav"), "start": 0.5, "fade": 0.0},
        ],
        # 11. 片段缺少 start 字段 -> 字段错误终止
        "edit_missing_field.json": [
            {"file": w("clip_a.wav"), "fade": 0.0},
        ],
        # 11b. 顶层缺少 clips 列表
        "edit_no_clips.json": {"segments": []},
        # 14. 第二段起点与首段相同 -> 跳过该段，不补静音
        "edit_same_start.json": [
            {"file": w("clip_a.wav"), "start": 0.0, "fade": 0.0},
            {"file": w("clip_b.wav"), "start": 0.0, "fade": 0.0},
        ],
        # 14b. 首个片段起点不为 0 -> 不补静音，从该起点开始计时
        "edit_first_offset.json": [
            {"file": w("clip_a.wav"), "start": 3.0, "fade": 0.0},
            {"file": w("clip_b.wav"), "start": 3.8, "fade": 0.2},
        ],
        # 综合场景：成功 + 格式跳过 + 缺失跳过 + 淡化
        "edit_mixed.json": [
            {"file": w("clip_a.wav"), "start": 0.0, "fade": 0.0},
            {"file": w("stereo48k.wav"), "start": 0.5, "fade": 0.1},
            {"file": w("missing_x.wav"), "start": 1.0, "fade": 0.0},
            {"file": w("clip_b.wav"), "start": 1.5, "fade": 0.3},
        ],
    }

    for name, payload in scenarios.items():
        write_json(os.path.join(workspace, name), payload)

    return workspace


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    target = sys.argv[1] if len(sys.argv) > 1 else "demo_workspace"
    build(os.path.abspath(target))
    print("样本与场景已生成到: %s" % os.path.abspath(target))
