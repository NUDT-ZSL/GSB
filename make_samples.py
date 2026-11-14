# -*- coding: utf-8 -*-
"""
make_samples.py
===============
生成可复现的本地样例数据（帧清单 + 逐帧 PNG），覆盖：
  正常帧、小尺寸帧（验居中）、带半透明边缘的帧（验黑边）、超尺寸帧、
  损坏 PNG、全透明帧、延时非法(0)帧、延时越界帧、缺文件条目。

用法：
  python -I make_samples.py            # 默认输出到 ./samples
  python -I make_samples.py --out 目录
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

CANVAS_W, CANVAS_H = 64, 64
BG = "#0066cc"  # 清单背景色（网页安全色，GIF 量化后可逐像素比对）


def solid(path: Path, size, rgb) -> None:
    Image.new("RGBA", size, rgb + (255,)).save(path)


def transparent(path: Path, size) -> None:
    Image.new("RGBA", size, (0, 0, 0, 0)).save(path)


def semi_transparent_edge(path: Path) -> None:
    """透明画布上的方块：中心不透明、外圈 alpha=128 半透明、四周完全透明（用于黑边渗色验收）。"""
    img = Image.new("RGBA", (40, 40), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rectangle([8, 8, 31, 31], fill=(255, 0, 0, 128))
    d.rectangle([12, 12, 27, 27], fill=(255, 0, 0, 255))
    img.save(path)


def make_samples(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    frames_dir = out / "frames"
    frames_dir.mkdir(exist_ok=True)

    # 正常帧（颜色互不相同且均为网页安全色，便于 GIF 帧序逐像素识别）
    solid(frames_dir / "f01.png", (64, 64), (255, 0, 0))
    solid(frames_dir / "f02.png", (64, 64), (0, 204, 0))
    solid(frames_dir / "f03.png", (64, 64), (0, 0, 255))
    # 小尺寸帧：32x24，验居中与等边距
    solid(frames_dir / "f04_small.png", (32, 24), (255, 204, 0))
    # 半透明边缘帧：验透明通道预览无纯黑渗色
    semi_transparent_edge(frames_dir / "f05_edge.png")
    # 全透明帧（空帧）
    transparent(frames_dir / "f06_empty.png", (64, 64))
    # 超尺寸帧：72x64，宽超出画布
    solid(frames_dir / "f07_oversize.png", (72, 64), (255, 0, 255))
    # 损坏 PNG：写入截断的无效字节
    (frames_dir / "f08_corrupt.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
    # 延时非法帧（文件本身正常，delay_ms=0）
    solid(frames_dir / "f09_delay0.png", (64, 64), (0, 204, 204))
    # 延时越界帧（文件本身正常，delay_ms=99999 超上界）
    solid(frames_dir / "f10_delaybig.png", (64, 64), (204, 204, 0))
    # 缺文件条目引用 f11_missing.png —— 故意不生成该文件
    solid(frames_dir / "f12_after_gap.png", (64, 64), (255, 153, 0))

    manifest = {
        "canvas": {"width": CANVAS_W, "height": CANVAS_H},
        "loop_count": 2,
        "background_color": BG,
        "frames": [
            {"file": "frames/f01.png", "delay_ms": 100},
            {"file": "frames/f02.png", "delay_ms": 200},
            {"file": "frames/f03.png", "delay_ms": 300},
            {"file": "frames/f04_small.png", "delay_ms": 100},
            {"file": "frames/f05_edge.png", "delay_ms": 100},
            {"file": "frames/f06_empty.png", "delay_ms": 100},
            {"file": "frames/f07_oversize.png", "delay_ms": 100},
            {"file": "frames/f08_corrupt.png", "delay_ms": 100},
            {"file": "frames/f09_delay0.png", "delay_ms": 0},
            {"file": "frames/f10_delaybig.png", "delay_ms": 99999},
            {"file": "frames/f11_missing.png", "delay_ms": 100},
            {"file": "frames/f12_after_gap.png", "delay_ms": 100},
        ],
    }
    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"样例已生成：{out}（{len(manifest['frames'])} 个帧条目，其中 f11 故意缺文件）")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="samples")
    args = ap.parse_args()
    make_samples(Path(args.out))
