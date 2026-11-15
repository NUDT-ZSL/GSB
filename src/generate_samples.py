# -*- coding: utf-8 -*-
"""确定性样本生成器：本地构造音画清单 JSON 与毫秒节拍样本。

所有样本均由固定常量生成（无随机数、无时钟依赖），重复运行产物逐字节一致。
生成清单：
  normal_25fps.json      常规样本：毫秒节拍 + 帧号转场混用，含已知被吞节拍
  boundary_24fps.json    边界样本：0 帧节拍、转场首尾边界、重叠镜头、越界节拍
  anomalies_30fps.json   异常样本：全遮短镜头、重叠镜头、越界节拍
  invalid_zero_fps.json  非法：帧率为 0
  invalid_negative_duration.json 非法：总时长为负
  invalid_broken.json    非法：不是合法 JSON

用法：
  python -I generate_samples.py <samples_dir>
"""
from __future__ import annotations

import json
import os
import sys


def _write_json(path: str, payload: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


def build_samples() -> dict[str, dict | str]:
    # ============ 1. 常规 25fps：40ms = 1 帧，便于手算 ============
    normal = {
        "fps": 25,
        "duration_ms": 10000,          # floor(10000*25/1000) = 250 帧
        "shots": [
            {"id": "S1", "start_frame": 0,   "end_frame": 60},   # 60 帧
            {"id": "S2", "start_frame": 60,  "end_frame": 120},  # 60 帧
            {"id": "S3", "start_frame": 120, "end_frame": 150},  # 30 帧（短镜头）
            {"id": "S4", "start_frame": 150, "end_frame": 250},  # 100 帧
        ],
        "transitions": [
            # 完全落在 S1 内部，不碰任何镜头首尾 -> 不算首尾缺帧
            {"id": "T_in",   "start_frame": 30,  "end_frame": 40},
            # 压在 S2 头部（S2 前 10 帧被遮）与 S1 尾部（S1 末尾 5 帧被遮）
            {"id": "T_head", "start_frame": 55,  "end_frame": 70},
            # 压在 S3 头部与 S2 尾部
            {"id": "T_mid",  "start_frame": 110, "end_frame": 130},
        ],
        # 毫秒节拍（与帧号转场混用）；帧号 = floor(t*25/1000)
        # 明确含非整帧毫秒值 1234ms -> 30 帧、9999ms -> 249 帧
        "beats_ms": [0, 40, 80, 1000, 1234, 1400, 2200, 2400, 3000,
                     4400, 4800, 5000, 9999],
    }

    # ============ 2. 边界 24fps：0 帧节拍、转场边界、重叠、越界 ============
    boundary = {
        "fps": 24,
        "duration_ms": 5000,           # 120 帧
        "shots": [
            {"id": "B1", "start_frame": 0,  "end_frame": 50},
            {"id": "B2", "start_frame": 45, "end_frame": 90},   # 与 B1 重叠 5 帧
            {"id": "B3", "start_frame": 90, "end_frame": 120},
        ],
        "transitions": [
            # 从 0 帧开始：遮住 B1 头部 10 帧；节拍 0 帧落在其中
            {"id": "TB0", "start_frame": 0,  "end_frame": 10},
            # 半开区间边界：帧 30 不属于该转场
            {"id": "TB1", "start_frame": 20, "end_frame": 30},
            # 压 B3 尾部 15 帧；B3 均匀取样末点 116 落在其中，标记图与报告对齐
            {"id": "TB2", "start_frame": 105, "end_frame": 120},
        ],
        "beats_ms": [
            0,        # 帧 0（0 帧边界节拍，被 TB0 吞）
            125,      # 帧 3（被 TB0 吞）
            833.34,   # floor(20000.16/1000)=20 帧（被 TB1 吞，区间起点）
            1250,     # 帧 30（恰在 TB1 结束边界，不被吞）
            4999.5,   # floor(119988/1000)=119 帧（被 TB2 吞，最后一帧）
            5000,     # floor(120000/1000)=120 帧 -> 越界
            6000,     # 帧 144 -> 越界
        ],
    }

    # ============ 3. 异常 30fps：全遮短镜头、重叠、越界 ============
    anomalies = {
        "fps": 30,
        "duration_ms": 3000,           # 90 帧
        "shots": [
            {"id": "A1", "start_frame": 0,  "end_frame": 30},
            {"id": "A2", "start_frame": 25, "end_frame": 35},  # 与 A1 重叠 5 帧
            # 短镜头，仅 8 帧，且被转场完整覆盖
            {"id": "A3", "start_frame": 40, "end_frame": 48},
            {"id": "A4", "start_frame": 50, "end_frame": 90},
        ],
        "transitions": [
            {"id": "TA1", "start_frame": 0,  "end_frame": 10},  # A1 头部缺 10
            {"id": "TA2", "start_frame": 40, "end_frame": 48},  # 全覆盖 A3
            {"id": "TA3", "start_frame": 85, "end_frame": 95},  # A4 尾部缺 5，且越总时长
        ],
        "beats_ms": [
            0,       # 帧 0，被 TA1 吞
            1000,    # 帧 30，落在 A2
            1400,    # 帧 42，被 TA2 吞（A3 内）
            2999,    # floor(89970/1000)=89 帧，被 TA3 吞
            3000,    # 帧 90 -> 越界
            -100,    # floor(-3000/1000)=-3 帧 -> 越界（负时间戳，真 floor）
        ],
    }

    invalid_zero_fps = {"fps": 0, "duration_ms": 1000, "shots": [],
                        "transitions": [], "beats_ms": []}
    invalid_negative = {"fps": 25, "duration_ms": -1, "shots": [],
                        "transitions": [], "beats_ms": []}

    return {
        "normal_25fps.json": normal,
        "boundary_24fps.json": boundary,
        "anomalies_30fps.json": anomalies,
        "invalid_zero_fps.json": invalid_zero_fps,
        "invalid_negative_duration.json": invalid_negative,
        # 非法 JSON：直接写裸文本，不经 json.dump
        "invalid_broken.json": "{ this is : not valid json,,, ",
    }


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("用法: python -I generate_samples.py <samples_dir>", file=sys.stderr)
        return 2
    out_dir = argv[1]
    os.makedirs(out_dir, exist_ok=True)
    for name, payload in build_samples().items():
        path = os.path.join(out_dir, name)
        if isinstance(payload, str):
            with open(path, "w", encoding="utf-8") as f:
                f.write(payload)
        else:
            _write_json(path, payload)
        print(f"已生成 {path}")
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv))
