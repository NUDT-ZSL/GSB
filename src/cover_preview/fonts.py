"""本地字体解析。

优先使用清单/命令行显式指定的字体；否则按平台常见中文字体回退。
仅使用本地文件，不访问网络。
"""
from __future__ import annotations

from pathlib import Path

from PIL import ImageFont

_FALLBACK_FONTS = [
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simsun.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
]


class FontError(RuntimeError):
    """找不到可用本地字体。"""


def resolve_font(preferred: str | Path | None = None) -> Path:
    """返回一个存在且可被 Pillow 加载的字体文件路径。"""
    candidates: list[Path] = []
    if preferred:
        candidates.append(Path(preferred))
    candidates.extend(Path(p) for p in _FALLBACK_FONTS)

    for path in candidates:
        if path.is_file():
            try:
                ImageFont.truetype(str(path), 20)
            except OSError:
                continue
            return path
    raise FontError(
        "未找到可用的本地字体文件，请通过清单 font 字段或 --font 参数指定。"
    )
