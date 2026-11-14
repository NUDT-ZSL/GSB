"""确定性生成演示所需的全部输入（无需网络、无需第三方音频库）。

生成内容（固定随机种子，重复运行字节级一致）::

    demo/input/
      soundtrack_mono.wav      16 kHz / 16 bit / 单声道，10 秒，鼓点在整秒处
      soundtrack_stereo.wav    同内容双声道版本，用于演示需求 1 的拒绝逻辑
      soundtrack_corrupt.wav   带 RIFF 伪装的损坏文件，用于演示需求 7
      shots_main.json          6 条镜头：含零时长 / 缺时长，总长超配乐 1 秒
      shots_three.json         3 条正常镜头，对应需求 5 的三张预览图
      shots_overhang.json      总长恰好超配乐 1 秒的清单，对应需求 3

合成思路：底噪 + 低频正弦铺底，每个鼓点处叠加短促高频"击镲"与
指数衰减的低频"鼓身"，起振点与指定毫秒严格对齐。
"""

from __future__ import annotations

import json
import math
import os
import random
import struct
import wave

SR = 16000
BITS = 16
DURATION_MS = 10_000
BEAT_TIMES_MS = [0, 1000, 2000, 3000, 4000, 5000, 6000, 7000, 8000, 9000]
SEED = 20261114


def _synth_mono_samples() -> list[int]:
    rng = random.Random(SEED)
    total = SR * DURATION_MS // 1000
    samples = [0.0] * total

    # 极轻底噪 + 低频铺底，模拟真实配乐但不制造额外起振点
    for n in range(total):
        t = n / SR
        samples[n] = 0.012 * math.sin(2 * math.pi * 110 * t) + rng.uniform(-0.004, 0.004)

    for beat_ms in BEAT_TIMES_MS:
        start = SR * beat_ms // 1000
        # 鼓身：60 Hz 指数衰减，约 250 ms 衰减完
        body_len = int(SR * 0.25)
        for k in range(body_len):
            idx = start + k
            if idx >= total:
                break
            t = k / SR
            env = math.exp(-t * 18.0)
            samples[idx] += 0.85 * env * math.sin(2 * math.pi * 60 * t)
        # 击镲：2500 Hz 短瞬态，约 40 ms，能量集中在起振瞬间
        click_len = int(SR * 0.04)
        for k in range(click_len):
            idx = start + k
            if idx >= total:
                break
            t = k / SR
            env = math.exp(-t * 90.0)
            samples[idx] += 0.5 * env * math.sin(2 * math.pi * 2500 * t)

    # 限幅并转 16-bit 小端有符号
    out = []
    for v in samples:
        v = max(-1.0, min(1.0, v))
        out.append(int(round(v * 32767)))
    return out


def _write_mono_wav(path: str, samples: list[int]) -> None:
    with wave.open(path, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SR)
        frames = struct.pack(f"<{len(samples)}h", *samples)
        wav.writeframes(frames)


def _write_stereo_wav(path: str, samples: list[int]) -> None:
    with wave.open(path, "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(SR)
        interleaved: list[int] = []
        for v in samples:
            interleaved.extend((v, v))
        wav.writeframes(struct.pack(f"<{len(interleaved)}h", *interleaved))


def _write_corrupt_wav(path: str) -> None:
    # RIFF/WAVE 外壳存在，但 fmt 块声明长度远超文件实际大小，
    # wave 模块 skip 时 seek 越界抛 RuntimeError，稳定触发"读取失败"
    rng = random.Random(SEED + 1)
    payload = bytes(rng.randrange(0, 256) for _ in range(512))
    with open(path, "wb") as handle:
        handle.write(b"RIFF")
        handle.write(struct.pack("<I", 12 + len(payload)))
        handle.write(b"WAVE")
        handle.write(b"fmt ")
        handle.write(struct.pack("<I", 999_999))  # 声明 999999 字节，实际仅 512
        handle.write(payload)


def _write_json(path: str, data) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def generate(base_dir: str) -> dict:
    in_dir = os.path.join(base_dir, "input")
    os.makedirs(in_dir, exist_ok=True)

    mono = os.path.join(in_dir, "soundtrack_mono.wav")
    stereo = os.path.join(in_dir, "soundtrack_stereo.wav")
    corrupt = os.path.join(in_dir, "soundtrack_corrupt.wav")
    shots_main = os.path.join(in_dir, "shots_main.json")
    shots_three = os.path.join(in_dir, "shots_three.json")
    shots_overhang = os.path.join(in_dir, "shots_overhang.json")

    samples = _synth_mono_samples()
    _write_mono_wav(mono, samples)
    _write_stereo_wav(stereo, samples)
    _write_corrupt_wav(corrupt)

    # 主清单：6 条，含 1 条零时长、1 条缺时长；有效总长 11000 ms，超配乐 1000 ms
    _write_json(
        shots_main,
        [
            {"title": "开场航拍", "duration_ms": 2500},
            {"title": "城市快切", "duration_ms": 1500},
            {"title": "人物特写", "duration_ms": 0},
            {"title": "产品全景", "duration_ms": 2000},
            {"title": "用户访谈"},
            {"title": "品牌落版", "duration_ms": 5000},
        ],
    )

    # 三镜头清单：全部有效，对应需求 5 的三张预览图
    _write_json(
        shots_three,
        [
            {"title": "开场航拍", "duration_ms": 2200},
            {"title": "产品全景", "duration_ms": 3300},
            {"title": "品牌落版", "duration_ms": 4500},
        ],
    )

    # 超时长清单：总长 11000 ms，恰好超配乐 1 秒（需求 3 的 1 秒设定）
    _write_json(
        shots_overhang,
        [
            {"title": "第一幕", "duration_ms": 3000},
            {"title": "第二幕", "duration_ms": 4000},
            {"title": "第三幕", "duration_ms": 4000},
        ],
    )

    return {
        "mono": mono,
        "stereo": stereo,
        "corrupt": corrupt,
        "shots_main": shots_main,
        "shots_three": shots_three,
        "shots_overhang": shots_overhang,
        "beat_times_ms": BEAT_TIMES_MS,
        "sample_rate": SR,
        "bits": BITS,
        "duration_ms": DURATION_MS,
    }


if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    info = generate(here)
    print("演示输入已生成：")
    for key, value in info.items():
        print(f"  {key}: {value}")
