#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""make_samples — 确定性生成业务样例（SRT 字幕与 JSON 配置）。

不依赖网络与随机数，重复运行生成内容完全一致。
输出到 samples/ 目录：
  basic.srt            3 条正常字幕（基础解析用例）
  full.srt             覆盖倒置、重叠、超宽、空白、多行等全部核对场景
  config.json          25fps / 1920x1080 / 边距 0.05 的标准配置
  config_colors.json   自定义底色与文字颜色的配置
  config_missing.json  缺少 frame_rate 的非法配置（用于失败路径演示）
"""

import json
from pathlib import Path

OUT = Path(__file__).resolve().parent / "samples"


def srt_block(index, start, end, text):
    return f"{index}\n{start} --> {end}\n{text}\n"


def main():
    OUT.mkdir(parents=True, exist_ok=True)

    # ---- basic.srt：3 条正常字幕 -------------------------------------------
    basic = "\n".join([
        srt_block(1, "00:00:01,000", "00:00:02,000", "你好，世界。"),
        srt_block(2, "00:00:03,000", "00:00:04,500", "第二条字幕，用于核对帧号换算。"),
        srt_block(3, "00:00:05,000", "00:00:06,000", "最后一条。"),
    ])
    (OUT / "basic.srt").write_text(basic, encoding="utf-8")

    # ---- full.srt：覆盖全部核对场景 ----------------------------------------
    long_31 = "春眠不觉晓处处闻啼鸟夜来风雨声花落知多少白日依山尽"  # 31 个全角字符
    overflow_text = "超宽字幕示例" * 7  # 42 个全角字符，48px 下宽 2016px > 1728px
    full = "\n".join([
        # 1: 正常，1.000s-2.000s @25fps -> 帧 25-49，共 25 帧
        srt_block(1, "00:00:01,000", "00:00:02,000", "你好，世界。"),
        # 2: 时间倒置 -> invalid
        srt_block(2, "00:00:03,000", "00:00:02,500", "这条字幕时间倒置。"),
        # 3 与 4: 显示区间重叠 10 帧（帧 100-124 与 115-139）
        srt_block(3, "00:00:04,000", "00:00:05,000", "第三条字幕与下一条重叠。"),
        srt_block(4, "00:00:04,600", "00:00:05,600", "第四条字幕与上一条重叠。"),
        # 5: 31 个全角字符 -> 估算 2 行，宽度在安全区内
        srt_block(5, "00:00:06,000", "00:00:08,000", long_31),
        # 6: 超宽字幕，超出安全区 288px
        srt_block(6, "00:00:09,000", "00:00:11,000", overflow_text),
        # 7: 空白文本 -> invalid，不渲染预览
        srt_block(7, "00:00:12,000", "00:00:13,000", "   "),
        # 8: 正常末条
        srt_block(8, "00:00:14,000", "00:00:15,000", "核对结束，谢谢。"),
    ])
    (OUT / "full.srt").write_text(full, encoding="utf-8")

    # ---- 配置 ---------------------------------------------------------------
    config = {
        "frame_rate": 25,
        "width": 1920,
        "height": 1080,
        "margin_ratio": 0.05,
        "font_size": 48,
        "chars_per_line": 18,
    }
    (OUT / "config.json").write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    config_colors = dict(config, background_color="#102030", text_color="#FFE080")
    (OUT / "config_colors.json").write_text(
        json.dumps(config_colors, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    missing = {k: v for k, v in config.items() if k != "frame_rate"}
    (OUT / "config_missing.json").write_text(
        json.dumps(missing, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    for p in sorted(OUT.iterdir()):
        print(f"生成: {p}")


if __name__ == "__main__":
    main()
