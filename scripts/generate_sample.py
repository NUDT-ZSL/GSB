"""确定性生成技能清单样例（无随机、无外部素材）。

产出：
  samples/skills_manifest.json   主清单（合法项 + 各类异常项，顺序打乱）
  samples/not_array_object.json  顶层为对象（需求 9 反例）
  samples/not_array_text.json    普通文本（需求 9 反例）
"""
from __future__ import annotations

import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLE_DIR = os.path.join(os.path.dirname(HERE), "samples")


def build_manifest() -> list[dict]:
    """手工编排的确定性样例，覆盖全部异常分支。位置序号即数组下标 +1。"""
    return [
        # 1 烈焰斩：三段关键帧 + 含越界音效（2200 > 2000）
        {
            "name": "烈焰斩",
            "duration_ms": 2000,
            "canvas": {"width": 1280, "height": 720},
            "camera_keyframes": [
                {"time_ms": 0, "zoom": 1.0, "center": [640, 360]},
                {"time_ms": 800, "zoom": 1.6, "center": [400, 300]},
                {"time_ms": 1600, "zoom": 1.2, "center": [700, 420]},
            ],
            "sounds": [
                400,                                   # 线性段，偏离
                800,                                   # 踩中关键帧，精准
                {"t": 1200, "label": "爆裂音"},          # 线性段中点
                {"t": 2200, "label": "收尾误点音"},       # 越界
            ],
        },
        # 2 冰霜新星：首帧前钳制（音效 0 与负值）
        {
            "name": "冰霜新星",
            "duration_ms": 1500,
            "canvas": {"width": 1024, "height": 640},
            "camera_keyframes": [
                {"time_ms": 300, "zoom": 1.1, "center": [512, 320]},
                {"time_ms": 1000, "zoom": 2.0, "center": [300, 200]},
            ],
            "sounds": [
                {"t": -100, "label": "前置误触音"},       # 越界（负值）
                0,                                     # 首帧之前：保持首帧值
                650,                                   # 线性段中点
            ],
        },
        # 3 雷霆一击：末帧之后钳制 + 贴近档
        {
            "name": "雷霆一击",
            "duration_ms": 1200,
            "canvas": {"width": 900, "height": 600},
            "camera_keyframes": [
                {"time_ms": 0, "zoom": 1.0, "center": [450, 300]},
                {"time_ms": 500, "zoom": 1.8, "center": [200, 400]},
            ],
            "sounds": [
                {"t": 620, "label": "雷鸣"},             # 间隔 120ms，贴近
                1200,                                  # 末帧之后：保持末帧值
            ],
        },
        # 4 空名技能：以位置序号 #04 作为页名/文件名（需求 12）
        {
            "name": "   ",
            "duration_ms": 1000,
            "canvas": {"width": 800, "height": 560},
            "camera_keyframes": [
                {"time_ms": 0, "zoom": 1.0, "center": [400, 280]},
                {"time_ms": 1000, "zoom": 1.0, "center": [400, 280]},
            ],
            "sounds": [0, 500, 1000],
        },
        # 5 缺少 sounds 字段 -> 跳过名单
        {
            "name": "残缺招式",
            "duration_ms": 1000,
            "canvas": {"width": 800, "height": 600},
            "camera_keyframes": [
                {"time_ms": 0, "zoom": 1.0, "center": [400, 300]}
            ],
        },
        # 6 duration_ms 类型错误 -> 跳过名单
        {
            "name": "类型错误招式",
            "duration_ms": "1500",
            "canvas": {"width": 800, "height": 600},
            "camera_keyframes": [
                {"time_ms": 0, "zoom": 1.0, "center": [400, 300]}
            ],
            "sounds": [100],
        },
        # 7 总时长为 0 -> 未完成名单（需求 7）
        {
            "name": "风刃",
            "duration_ms": 0,
            "canvas": {"width": 800, "height": 600},
            "camera_keyframes": [
                {"time_ms": 0, "zoom": 1.0, "center": [400, 300]}
            ],
            "sounds": [0],
        },
        # 8 画布高度为 0 -> 未完成名单
        {
            "name": "暗影突袭",
            "duration_ms": 1800,
            "canvas": {"width": 1000, "height": 0},
            "camera_keyframes": [
                {"time_ms": 0, "zoom": 1.3, "center": [500, 300]},
                {"time_ms": 900, "zoom": 1.9, "center": [250, 180]},
            ],
            "sounds": [450, 900],
        },
        # 9 圣盾术：正常多音效，乱序时刻用于验证页内升序（需求 5）
        {
            "name": "圣盾术",
            "duration_ms": 1600,
            "canvas": {"width": 1100, "height": 680},
            "camera_keyframes": [
                {"time_ms": 0, "zoom": 1.0, "center": [550, 340]},
                {"time_ms": 400, "zoom": 1.4, "center": [550, 260]},
                {"time_ms": 1200, "zoom": 1.1, "center": [700, 340]},
            ],
            "sounds": [
                {"t": 1200, "label": "盾击"},
                {"t": 120, "label": "念咒"},
                {"t": 400, "label": "展开"},
                {"t": 1550, "label": "消散"},
            ],
        },
        # 10 关键帧 center 缺项 -> 跳过名单
        {
            "name": "数据损坏招式",
            "duration_ms": 1000,
            "canvas": {"width": 800, "height": 600},
            "camera_keyframes": [
                {"time_ms": 0, "zoom": 1.0, "center": [400]}
            ],
            "sounds": [],
        },
        # 11 条目不是对象 -> 跳过名单
        "这不是一个技能对象",
    ]


def main() -> None:
    os.makedirs(SAMPLE_DIR, exist_ok=True)
    manifest = build_manifest()

    main_path = os.path.join(SAMPLE_DIR, "skills_manifest.json")
    with open(main_path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)

    obj_path = os.path.join(SAMPLE_DIR, "not_array_object.json")
    with open(obj_path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump({"name": "顶层是对象不是数组"}, fh, ensure_ascii=False, indent=2)

    text_path = os.path.join(SAMPLE_DIR, "not_array_text.json")
    with open(text_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("这只是一段普通文本，不是 JSON\n")

    print("已生成样例：")
    for path in (main_path, obj_path, text_path):
        print(" -", os.path.abspath(path))


if __name__ == "__main__":
    main()
