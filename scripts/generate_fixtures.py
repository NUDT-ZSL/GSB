#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
generate_fixtures.py —— 生成确定性的测试素材（无第三方库、无网络）

在指定目录下生成：
  audio/main.wav                 主干：44100Hz 单声道 16-bit PCM，12.000s
  audio/tone_a.wav               提示音 A：44100Hz，1.000s
  audio/tone_b_22050.wav         提示音 B：22050Hz（与主干采样率不一致），0.500s
  manifests/manifest_valid.json           正常清单（2 删除 + 2 插入）
  manifests/manifest_bad_clip.json        含起止倒置与越界删除片段
  manifests/manifest_dup_id.json          clip_id 重复
  manifests/manifest_missing_field.json   缺少必需字段
  manifests/manifest_bad_json.json        非法 JSON
  manifests/manifest_abs_tone.json        提示音使用绝对路径
  manifests/manifest_multi_main.json      主干字段为数组（引用多个主干）

用法：
  python -I scripts/generate_fixtures.py --out fixtures
重复运行生成的字节完全一致（纯公式波形、固定元数据）。
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from array import array
from pathlib import Path
from wave import open as wave_open

MAIN_RATE = 44100
LOW_RATE = 22050


def write_wav(path: Path, samples: array, rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave_open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(samples.tobytes())


def sine_mix(rate: int, duration_sec: float) -> array:
    """确定性混合正弦波，峰值约 15000，便于演示峰值归一化。"""
    n = int(round(rate * duration_sec))
    out = array("h")
    for i in range(n):
        t = i / rate
        value = 12000.0 * math.sin(2 * math.pi * 220.0 * t) + 3000.0 * math.sin(
            2 * math.pi * 880.0 * t
        )
        out.append(int(round(value)))
    return out


def pure_tone(rate: int, duration_sec: float, freq: float, amp: int) -> array:
    n = int(round(rate * duration_sec))
    out = array("h")
    for i in range(n):
        t = i / rate
        out.append(int(round(amp * math.sin(2 * math.pi * freq * t))))
    return out


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fp:
        json.dump(obj, fp, ensure_ascii=False, indent=2)
        fp.write("\n")


def build_manifests(audio_dir_name: str, fixture_root: Path) -> dict:
    # 清单位于 manifests/ 目录，音频位于同级 audio/ 目录，
    # 因此相对清单文件的引用为 ../audio/...（相对路径，不违反绝对路径禁令）
    main_ref = f"../{audio_dir_name}/main.wav"
    tone_a = f"../{audio_dir_name}/tone_a.wav"
    tone_b = f"../{audio_dir_name}/tone_b_22050.wav"

    valid = {
        "main_recording": main_ref,
        "clips": [
            {"clip_id": "c01-delete-head", "type": "delete",
             "start_sec": 1.0, "end_sec": 2.0},
            {"clip_id": "c02-insert-a", "type": "insert_tone",
             "at_sec": 3.0, "tone": tone_a},
            {"clip_id": "c03-delete-mid", "type": "delete",
             "start_sec": 5.0, "end_sec": 6.0},
            {"clip_id": "c04-insert-b-lowrate", "type": "insert_tone",
             "at_sec": 8.0, "tone": tone_b},
        ],
    }

    bad_clip = {
        "main_recording": main_ref,
        "clips": [
            {"clip_id": "b01-delete-ok", "type": "delete",
             "start_sec": 1.0, "end_sec": 2.0},
            # 起点晚于终点 -> 该片段未处理，其余继续
            {"clip_id": "b02-reversed", "type": "delete",
             "start_sec": 4.0, "end_sec": 3.0},
            # 超出主干时长 -> 该片段未处理
            {"clip_id": "b03-out-of-range", "type": "delete",
             "start_sec": 100.0, "end_sec": 101.0},
            {"clip_id": "b04-insert-ok", "type": "insert_tone",
             "at_sec": 5.0, "tone": tone_a},
        ],
    }

    dup_id = {
        "main_recording": main_ref,
        "clips": [
            {"clip_id": "DUP", "type": "delete", "start_sec": 1.0, "end_sec": 2.0},
            {"clip_id": "DUP", "type": "delete", "start_sec": 3.0, "end_sec": 4.0},
        ],
    }

    missing_field = {
        "main_recording": main_ref,
        "clips": [
            {"clip_id": "m01", "type": "delete", "start_sec": 1.0},  # 缺 end_sec
        ],
    }

    abs_tone = {
        "main_recording": main_ref,
        "clips": [
            {"clip_id": "a01-abs", "type": "insert_tone", "at_sec": 1.0,
             "tone": str((fixture_root / audio_dir_name / "tone_a.wav").resolve())},
        ],
    }

    multi_main = {
        "main_recording": [main_ref, f"../{audio_dir_name}/other_main.wav"],
        "clips": [
            {"clip_id": "x01", "type": "delete", "start_sec": 1.0, "end_sec": 2.0},
        ],
    }

    return {
        "manifest_valid.json": valid,
        "manifest_bad_clip.json": bad_clip,
        "manifest_dup_id.json": dup_id,
        "manifest_missing_field.json": missing_field,
        "manifest_abs_tone.json": abs_tone,
        "manifest_multi_main.json": multi_main,
    }


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="生成播客粗剪确定性测试素材")
    parser.add_argument("--out", required=True, help="素材输出目录")
    args = parser.parse_args()

    root = Path(args.out)
    audio_dir = root / "audio"
    manifest_dir = root / "manifests"

    write_wav(audio_dir / "main.wav", sine_mix(MAIN_RATE, 12.0), MAIN_RATE)
    write_wav(audio_dir / "tone_a.wav", pure_tone(MAIN_RATE, 1.0, 440.0, 9000), MAIN_RATE)
    write_wav(audio_dir / "tone_b_22050.wav", pure_tone(LOW_RATE, 0.5, 660.0, 7000), LOW_RATE)

    for name, obj in build_manifests("audio", root).items():
        write_json(manifest_dir / name, obj)

    # 非法 JSON（截断的对象）
    with open(manifest_dir / "manifest_bad_json.json", "w", encoding="utf-8", newline="\n") as fp:
        fp.write('{\n  "main_recording": "../audio/main.wav",\n  "clips": [\n')

    print(f"素材已生成于: {root.resolve()}")
    for p in sorted(root.rglob("*")):
        if p.is_file():
            print(f"  {p.relative_to(root)}  ({p.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
