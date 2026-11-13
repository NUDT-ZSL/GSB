# -*- coding: utf-8 -*-
"""确定性生成小规模 WAV 样本与演示剪辑清单（仅标准库，无外部素材）。

用法:
    python make_samples.py [workspace_dir]   # 默认 ./workspace
生成:
    samples/ramp.wav        严格递增样本 1..22050 (0.5s)
    samples/padded.wav      首尾静音包裹的恒定信号
    samples/gain.wav        恒定 10000，用于增益/钳位
    samples/silence.wav     全零 (0.25s)
    samples/stereo.wav      立体声（非法候选）
    samples/rate48k.wav     48kHz（非法候选）
    manifest_demo.json      混合清单（有效+各类无效段）
    manifest_all_invalid.json  全部无效
    manifest_bad_field.json    reverse 传字符串（应报错）
"""
import json
import os
import struct
import sys
import wave

RATE = 44100


def write_wav(path, samples, nch=1, rate=RATE, sampwidth=2):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with wave.open(path, "wb") as w:
        w.setnchannels(nch)
        w.setsampwidth(sampwidth)
        w.setframerate(rate)
        if sampwidth == 2:
            w.writeframes(struct.pack("<%dh" % len(samples), *samples))
        else:
            w.writeframes(bytes(samples))


def build(workspace):
    sdir = os.path.join(workspace, "samples")
    os.makedirs(sdir, exist_ok=True)

    ramp = [i + 1 for i in range(RATE // 2)]                      # 1..22050 严格递增
    write_wav(os.path.join(sdir, "ramp.wav"), ramp)

    padded = [0] * 1000 + [5000] * 2000 + [100, -100] + [0] * 500  # 首尾静音
    write_wav(os.path.join(sdir, "padded.wav"), padded)

    gain = [10000] * 4000 + [20000] * 500 + [-20000] * 500        # 增益后触发钳位
    write_wav(os.path.join(sdir, "gain.wav"), gain)

    write_wav(os.path.join(sdir, "silence.wav"), [0] * (RATE // 4))

    stereo = []
    for i in range(1000):
        stereo += [1000, -1000]
    write_wav(os.path.join(sdir, "stereo.wav"), stereo, nch=2)

    write_wav(os.path.join(sdir, "rate48k.wav"), [3000] * 4800, rate=48000)

    def sp(name):
        return os.path.join(sdir, name)

    demo = {
        "output": os.path.join(workspace, "final.wav"),
        "report": os.path.join(workspace, "report.json"),
        "segments": [
            {"file": sp("ramp.wav"), "reverse": True},
            {"file": sp("padded.wav"), "silence_threshold": 100},
            {"file": sp("gain.wav"), "gain": 2.0},
            {"file": sp("missing.wav")},
            {"file": sp("stereo.wav")},
            {"file": sp("rate48k.wav")},
            {"file": sp("ramp.wav"), "start_ms": 0.0, "end_ms": 9999.0},
            {"file": sp("ramp.wav"), "start_ms": 400.0, "end_ms": 100.0},
        ],
    }
    all_invalid = {
        "output": os.path.join(workspace, "final_invalid.wav"),
        "report": os.path.join(workspace, "report_invalid.json"),
        "segments": [
            {"file": sp("missing.wav")},
            {"file": sp("stereo.wav")},
            {"file": sp("rate48k.wav")},
        ],
    }
    bad_field = {
        "output": os.path.join(workspace, "final_bad.wav"),
        "report": os.path.join(workspace, "report_bad.json"),
        "segments": [{"file": sp("ramp.wav"), "reverse": "yes"}],
    }
    for name, obj in [("manifest_demo.json", demo),
                      ("manifest_all_invalid.json", all_invalid),
                      ("manifest_bad_field.json", bad_field)]:
        with open(os.path.join(workspace, name), "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
    return workspace


if __name__ == "__main__":
    ws = build(sys.argv[1] if len(sys.argv) > 1 else "workspace")
    print("样本与清单已生成于: %s" % os.path.abspath(ws))
