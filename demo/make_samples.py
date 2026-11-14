# -*- coding: utf-8 -*-
"""生成演示用 SRT 样例（可重复执行，输出目录会被重建）。

用法:
    python demo/make_samples.py            # 在 demo/input 下生成样例
    python demo/make_samples.py <目标目录>  # 生成到指定目录

样例覆盖：合法文件、重叠 + 毫秒缺位 + 空文本 + 颠倒 + 乱序号、
BOM + 多行文本、无法解析的空文件、非 .srt 文件、较大批量文件。
"""

from __future__ import annotations

import sys
from pathlib import Path

DEMO_DIR = Path(__file__).resolve().parent
DEFAULT_TARGET = DEMO_DIR / "input"


def block(index: int, start: str, end: str, lines: list[str]) -> str:
    return f"{index}\n{start} --> {end}\n" + "\n".join(lines)


def srt(*blocks: str) -> str:
    return "\n\n".join(blocks) + "\n"


def make_legal() -> str:
    """完全合法：序号连续、时间规范、无重叠、无空文本。"""
    cues = [
        ("00:00:01,000", "00:00:02,500", "旁白：清晨的城市还没醒来。"),
        ("00:00:02,500", "00:00:05,000", "镜头推向街角的早餐摊。"),
        ("00:00:05,000", "00:00:07,200", "老板：今天还是老样子？"),
        ("00:00:07,200", "00:00:10,000", "旁白：热气在冷空气里散开。"),
    ]
    return srt(
        *(block(i, start, end, [text]) for i, (start, end, text) in enumerate(cues, 1))
    )


def make_messy() -> str:
    """重叠、毫秒不足三位、空文本、时间颠倒、序号不连续/重复。"""
    return srt(
        block(1, "00:00:01,000", "00:00:04,000", ["第一句：你好。"]),
        # 与上一条重叠，且毫秒只有两位
        block(2, "00:00:03,50", "00:00:06,000", ["第二句：我们在等你。"]),
        # 空文本条目
        block(2, "00:00:06,000", "00:00:07,000", ["   "]),
        # 结束早于开始
        block(9, "00:00:12,000", "00:00:09,000", ["这条时间颠倒了。"]),
        # 整段落在前一条内部（会触发“调整前一条终点”）
        block(9, "00:00:10,500", "00:00:13,000", ["第三句：别走太远。"]),
        block(7, "00:00:11,000", "00:00:12,000", ["第四句：好的。"]),
    )


def make_bom_multiline() -> bytes:
    """UTF-8 BOM + 多行字幕文本，内容本身合法。"""
    text = srt(
        block(1, "00:00:00,500", "00:00:03,000", ["第一行字幕", "第二行字幕"]),
        block(2, "00:00:03,000", "00:00:06,000", ["单行字幕"]),
    )
    return b"\xef\xbb\xbf" + text.encode("utf-8")


def make_large(count: int = 400) -> str:
    """较大批量文件：每 50 条插入一处 0.4s 重叠，序号连续。"""
    parts = []
    for i in range(count):
        start_ms = i * 2000
        end_ms = start_ms + 1600
        if i % 50 == 49:
            end_ms = start_ms + 2400  # 与下一条重叠 400ms
        parts.append(
            block(
                i + 1,
                ms_to_token(start_ms),
                ms_to_token(end_ms),
                [f"第 {i + 1} 条字幕"],
            )
        )
    return srt(*parts)


def ms_to_token(ms: int) -> str:
    hours, rem = divmod(ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    seconds, millis = divmod(rem, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def main(argv: list[str]) -> int:
    target = Path(argv[1]) if len(argv) > 1 else DEFAULT_TARGET
    target.mkdir(parents=True, exist_ok=True)

    for old in target.glob("*"):
        if old.is_file():
            old.unlink()

    (target / "01_legal.srt").write_text(make_legal(), encoding="utf-8", newline="\n")
    (target / "02_messy.srt").write_text(make_messy(), encoding="utf-8", newline="\n")
    (target / "03_bom_multiline.srt").write_bytes(make_bom_multiline())
    (target / "04_empty.srt").write_bytes(b"")
    (target / "05_large.srt").write_text(
        make_large(), encoding="utf-8", newline="\n"
    )
    (target / "说明.txt").write_text(
        "这不是字幕文件，工具应当忽略它。\n", encoding="utf-8", newline="\n"
    )

    print(f"样例已生成: {target}")
    for path in sorted(target.iterdir()):
        print(f"  {path.name:24s} {path.stat().st_size} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
