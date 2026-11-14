"""字体解析与加载。

全部使用 Pillow 的 FreeType 真实字体（ImageFont.truetype），
不做任何字宽估算；字体文件来自 Windows 自带字体目录。
"""
from __future__ import annotations

import os
from functools import lru_cache
from typing import Tuple

from PIL import ImageFont

WINDOWS_FONTS = os.environ.get("WINDIR", r"C:\Windows") + r"\Fonts"

# family -> (常规体文件, 粗体文件)。黑体/宋体无独立粗体文件时回退同一文件。
FONT_REGISTRY = {
    "microsoft_yahei": ("msyh.ttc", "msyhbd.ttc"),
    "simhei": ("simhei.ttf", "simhei.ttf"),
    "simsun": ("simsun.ttc", "simsun.ttc"),
    "arial": ("arial.ttf", "arialbd.ttf"),
    "calibri": ("calibri.ttf", "calibrib.ttf"),
}

DEFAULT_FAMILY = "microsoft_yahei"
VALID_WEIGHTS = ("regular", "bold")


class FontLoadError(Exception):
    """字体无法加载时抛出，消息为可写入报告的中文原因。"""


def _resolve_path(file_name: str) -> str:
    path = os.path.join(WINDOWS_FONTS, file_name)
    if not os.path.isfile(path):
        raise FontLoadError(f"字体文件不存在：{path}")
    return path


@lru_cache(maxsize=64)
def _load_cached(path: str, size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(path, size=size)
    except OSError as exc:  # pragma: no cover - 取决于本机字体
        raise FontLoadError(f"字体无法加载：{path}（{exc}）") from exc


def load_font(family: str, size: int, weight: str = "regular") -> ImageFont.FreeTypeFont:
    """按 family / 字号 / 字重加载真实字体。

    family 既可以是 FONT_REGISTRY 中的逻辑名，也可以是本机 .ttf/.ttf/.otf 路径。
    """
    if weight not in VALID_WEIGHTS:
        weight = "regular"
    bold = weight == "bold"

    if family in FONT_REGISTRY:
        regular_name, bold_name = FONT_REGISTRY[family]
        file_name = bold_name if bold else regular_name
        path = _resolve_path(file_name)
    elif isinstance(family, str) and os.path.splitext(family)[1].lower() in (".ttf", ".ttc", ".otf"):
        if not os.path.isfile(family):
            raise FontLoadError(f"字体文件不存在：{family}")
        path = family
    else:
        raise FontLoadError(f"未知字体族：{family!r}，可选值：{', '.join(sorted(FONT_REGISTRY))}")

    return _load_cached(path, int(size))


def measure_bbox(font: ImageFont.FreeTypeFont, text: str,
                 stroke_width: int) -> Tuple[int, int, int, int]:
    """返回文本相对渲染锚点(la=左上基线锚点)的真实墨迹包围盒。

    使用 FreeTypeFont.getbbox 取得真实字体度量，包含描边扩张，
    与 ImageDraw.text(..., anchor="la") 的渲染结果严格一致。
    """
    return font.getbbox(text, stroke_width=stroke_width, anchor="la")
