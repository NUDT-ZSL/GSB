# -*- coding: utf-8 -*-
"""确定性生成演示用的双轨单声道 16-bit PCM WAV 与剪辑清单样本。

仅使用标准库 wave/csv/array，不访问网络、不依赖第三方库。
默认输出到 ./demo 目录，也可通过命令行参数指定目录：
    python make_samples.py [输出目录]
重复运行生成的文件逐字节一致（样本值由帧序号确定性计算）。
"""

from __future__ import annotations

import array
import csv
import os
import sys
import wave
from pathlib import Path

RATE = 8000          # 共同采样率
N_FRAMES = 8000      # 每轨 1.000 秒


def _host_sample(i: int) -> int:
    """主机位：确定性正值序列，范围约 -1000..1000。"""
    return ((i * 37 + 11) % 2001) - 1000


def _guest_sample(i: int) -> int:
    """嘉宾位：确定性负值序列，与主机位逐帧不同，便于核对优先级。"""
    return -(((i * 53 + 7) % 2001) - 1000)


def write_mono_wav(path: Path, values, rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    samples = array.array("h", (int(v) for v in values))
    if sys.byteorder == "big":
        samples.byteswap()  # WAV 统一小端
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(samples.tobytes())


def write_demo_csv(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # 物理行号即报告中引用的行号；空行被跳过，非法行进拒绝列表。
    rows = [
        ["track", "start", "end"],        # 第 1 行：表头，自动识别跳过
        [],                               # 第 2 行：空行，跳过
        ["host", "0.0", "0.1"],           # 第 3 行：合法，裁主机位 0-800 帧
        [],                               # 第 4 行：空行，跳过
        ["host", "0.05", "0.2"],          # 第 5 行：与第 3 行重叠，合并为 0-1600
        ["guest", "0.3", "0.4"],          # 第 6 行：合法，裁嘉宾位 2400-3200
        ["guest", "0.5", "0.4"],          # 第 7 行：起止倒置，拒绝
        ["other", "0.1", "0.2"],          # 第 8 行：未知轨道名，拒绝
        ["host", "0.9", "1.5"],           # 第 9 行：终点越界（>8000 帧），拒绝
        ["host", "0.6"],                  # 第 10 行：字段缺失，拒绝
        [],                               # 第 11 行：空行，跳过
        ["嘉宾位", "0.6", "0.7"],         # 第 12 行：中文轨道名合法，4800-5600
    ]
    # utf-8 带 BOM 也能被工具读取（utf-8-sig），这里用纯 UTF-8 输出。
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerows(rows)


def main() -> int:
    out_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "demo")
    out_dir = out_dir.resolve()
    host_path = out_dir / "host_demo.wav"
    guest_path = out_dir / "guest_demo.wav"
    csv_path = out_dir / "edits_demo.csv"

    write_mono_wav(host_path, (_host_sample(i) for i in range(N_FRAMES)), RATE)
    write_mono_wav(guest_path, (_guest_sample(i) for i in range(N_FRAMES)), RATE)
    write_demo_csv(csv_path)

    print(f"已生成确定性样本（{RATE} Hz，每轨 {N_FRAMES} 帧 / 1.000 秒）：")
    for p in (host_path, guest_path, csv_path):
        print(f"  - {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
