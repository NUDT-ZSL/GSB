"""跨平台 TrueType 字体加载，失败时回退 Pillow 位图默认字体。"""
from __future__ import annotations

from pathlib import Path

from PIL import ImageFont

_CANDIDATES = (
    # Windows 常见中文字体
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/simsun.ttc",
    # macOS
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    # Linux 常见
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)


def load_font(size: int):
    """按候选列表加载 TrueType 字体；全部失败则回退位图默认字体。"""
    for candidate in _CANDIDATES:
        if Path(candidate).is_file():
            try:
                return ImageFont.truetype(candidate, size=size)
            except OSError:
                continue
    return ImageFont.load_default()
