#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""确定性生成核对工具所需的全部样本与配置（不使用随机数，可随时重建）。

生成内容：
  samples/main/       主样本：各 5 条标记，覆盖顺序颠倒、重复、越界、字段错误、未匹配
  samples/boundary/   边界样本：偏移恰好等于容差（不校正）、负时间越界
  samples/unmatched/  未匹配样本：音频/引擎单侧标记
  samples/sort_ties/  排序样本：两个等偏移标记验证名称字典序
  config/             配置 JSON（容差 80，总时长 60000）
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENCODING = "utf-8"


def write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding=ENCODING,
        newline="\n",
    )


def build_samples() -> None:
    # ---------------- 主样本（每表 5 条；规则 1~8、10 集中覆盖） ----------------
    # 音频表（5 条）：
    #   BGM_Start        1200  与引擎匹配，偏移 +150 -> 校正
    #   Camera_Shake     4000  与引擎匹配，偏移 -200 -> 校正（绝对值最大，排序第一）
    #   Footstep         2000  第 3 条 < 第 2 条 4000 -> 顺序异常；偏移 +50 容差内不校正
    #   Door_Open        3000  仅音频存在 -> 未匹配（音频）；与第 5 条同名
    #   Door_Open        2800  界内名称重复 -> 不可核对，仅首个 Door_Open 参与比对
    audio_main = [
        {"name": "BGM_Start", "time": 1200},
        {"name": "Camera_Shake", "time": 4000},
        {"name": "Footstep", "time": 2000},
        {"name": "Door_Open", "time": 3000},
        {"name": "Door_Open", "time": 2800},
    ]
    # 引擎表（5 条）：
    #   BGM_Start        1350  偏移 +150
    #   Footstep         2050  偏移 +50
    #   Light_Flash      4100  仅引擎存在 -> 未匹配（引擎）
    #   Camera_Shake     3800  第 4 条 < 第 3 条 4100 -> 顺序异常；偏移 -200
    #   （无名时间对）         缺少 name -> 字段缺失，不可核对
    engine_main = [
        {"name": "BGM_Start", "time": 1350},
        {"name": "Footstep", "time": 2050},
        {"name": "Light_Flash", "time": 4100},
        {"name": "Camera_Shake", "time": 3800},
        {"time": 5200},
    ]
    write_json(ROOT / "samples" / "main" / "audio_cues.json", audio_main)
    write_json(ROOT / "samples" / "main" / "engine_events.json", engine_main)

    # ---------------- 边界样本 ----------------
    # 引擎 1080 - 音频 1000 = 80，恰好等于容差 -> 不进校正清单
    # 引擎 3000 - 音频 3000 = 0
    # Side_Minus time=-1 -> 负时间越界；Side_Above time=99999 -> 超总时长上界越界
    audio_boundary = [
        {"name": "Edge_Equal", "time": 1000},
        {"name": "Edge_Zero", "time": 3000},
        {"name": "Side_Minus", "time": -1},
        {"name": "Side_Above", "time": 99999},
    ]
    engine_boundary = [
        {"name": "Edge_Equal", "time": 1080},
        {"name": "Edge_Zero", "time": 3000},
    ]
    write_json(ROOT / "samples" / "boundary" / "audio_cues.json", audio_boundary)
    write_json(ROOT / "samples" / "boundary" / "engine_events.json", engine_boundary)

    # ---------------- 未匹配样本（单侧标记，规则 6） ----------------
    audio_unmatched = [
        {"name": "Only_Audio_1", "time": 1000},
        {"name": "Only_Audio_2", "time": 2000},
        {"name": "Shared_Mark", "time": 3000},
    ]
    engine_unmatched = [
        {"name": "Shared_Mark", "time": 3000},
        {"name": "Only_Engine_1", "time": 4000},
    ]
    write_json(ROOT / "samples" / "unmatched" / "audio_cues.json", audio_unmatched)
    write_json(ROOT / "samples" / "unmatched" / "engine_events.json", engine_unmatched)

    # ---------------- 排序样本（规则 9：等偏移按名称升序） ----------------
    # Zebra_Two / Alpha_One 偏移均为 -120；按名称升序 Alpha_One 在前
    audio_sort = [
        {"name": "Zebra_Two", "time": 5000},
        {"name": "Alpha_One", "time": 4000},
        {"name": "Big_Offset", "time": 1000},
    ]
    engine_sort = [
        {"name": "Zebra_Two", "time": 4880},
        {"name": "Alpha_One", "time": 3880},
        {"name": "Big_Offset", "time": 1200},
    ]
    write_json(ROOT / "samples" / "sort_ties" / "audio_cues.json", audio_sort)
    write_json(ROOT / "samples" / "sort_ties" / "engine_events.json", engine_sort)

    # ---------------- 配置 ----------------
    config = {"tolerance_ms": 80, "total_duration_ms": 60000}
    write_json(ROOT / "config" / "config.sample.json", config)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    build_samples()
    print("样本与配置已在", ROOT, "下确定性重建完成")
