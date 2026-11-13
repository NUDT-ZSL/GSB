#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""业务操作样例: 模拟一期播客的后期拼接流程。

运行:
    python -I demo.py

场景: 从 4 秒素材中剪出三段口播, 分别加淡入/淡出, 整体提升到 130% 音量;
脚本中故意混入 1 个越界片段和 1 个淡变超标的片段，演示"失败片段不拖垮导出"。
产物在 ./outputs 下: demo_out.wav 与 demo_report.json。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.abspath(__file__))
INPUT_WAV = os.path.join(ROOT, "samples", "source_16k_mono.wav")
SCRIPT_PATH = os.path.join(ROOT, "outputs", "demo_script.json")
OUT_WAV = os.path.join(ROOT, "outputs", "demo_out.wav")
REPORT = os.path.join(ROOT, "outputs", "demo_report.json")

# 素材总长 4000ms。时间单位均为毫秒; null 表示该端不做淡变。
SCRIPT = {
    "volume": 130,
    "segments": [
        # 0: 开场白, 100ms 淡入
        {"start_ms": 0, "end_ms": 800, "fade_in_ms": 100, "fade_out_ms": None},
        # 1: 中段口播, 两端各 80ms 淡变
        {"start_ms": 1200, "end_ms": 2000, "fade_in_ms": 80, "fade_out_ms": 80},
        # 2: 越界片段 (素材只有 4000ms) -> 预期被拒绝
        {"start_ms": 3500, "end_ms": 5000, "fade_in_ms": None, "fade_out_ms": None},
        # 3: 结束语, 150ms 淡出
        {"start_ms": 2600, "end_ms": 3400, "fade_in_ms": None, "fade_out_ms": 150},
        # 4: 淡变合计 400ms >= 片段 400ms -> 预期被拒绝
        {"start_ms": 800, "end_ms": 1200, "fade_in_ms": 200, "fade_out_ms": 200},
    ],
}


def main() -> int:
    os.makedirs(os.path.dirname(SCRIPT_PATH), exist_ok=True)
    with open(SCRIPT_PATH, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(SCRIPT, fh, ensure_ascii=False, indent=2)
    print(f"编辑脚本已写出: {SCRIPT_PATH}")

    proc = subprocess.run(
        [sys.executable, "-I", os.path.join(ROOT, "podcast_editor.py"),
         INPUT_WAV, SCRIPT_PATH, OUT_WAV, REPORT],
        capture_output=True,
    )
    for enc in ("utf-8", "gbk"):
        try:
            stderr = proc.stderr.decode(enc)
            break
        except UnicodeDecodeError:
            stderr = proc.stderr.decode("utf-8", errors="replace")
    if proc.returncode != 0:
        print(stderr)
        return proc.returncode

    import wave
    with wave.open(OUT_WAV, "rb") as wf:
        print(
            f"\n输出 WAV: {wf.getnchannels()} 声道 / {wf.getsampwidth() * 8}bit / "
            f"{wf.getframerate()}Hz / {wf.getnframes()} 帧 / "
            f"{wf.getnframes() / wf.getframerate():.3f}s"
        )
    with open(REPORT, "r", encoding="utf-8") as fh:
        report = json.load(fh)
    print("\n报告摘要:")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
