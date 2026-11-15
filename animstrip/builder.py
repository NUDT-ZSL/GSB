"""条带合成：按帧序号顺序水平拼接，保留原始 alpha 通道。"""

from __future__ import annotations

from pathlib import Path

from PIL import Image

from .models import AnimationSpec, FrameSpec

MAX_FRAME_SIZE = 512
UNREADABLE_MARKER = "无法解码"


class FrameLoadError(Exception):
    """帧图片缺失或无法解码。"""


def build_strip(spec: AnimationSpec) -> tuple[Image.Image, list[FrameSpec]]:
    """按序号顺序加载各帧并水平拼接为 RGBA 条带。

    条带高度取最高帧（其余帧垂直居中、透明补边），宽度为各帧宽度之和。
    全程在 RGBA 模式下用 alpha_composite 粘贴，透明像素保持原 alpha，
    不会被填充为白色或黑色底色。

    任一帧缺失或无法解码时抛出 FrameLoadError，该动画不出条带。
    """
    ordered = sorted(spec.frames, key=lambda f: f.index)
    loaded: list[tuple[FrameSpec, Image.Image]] = []
    for frame in ordered:
        path = spec.resolve_frame_path(frame)
        if not path.is_file():
            raise FrameLoadError(f"帧图片缺失：{path}")
        try:
            with Image.open(path) as img:
                img.load()
                rgba = img.convert("RGBA")
        except Exception as exc:  # Pillow 对损坏文件会抛多种异常
            raise FrameLoadError(f"帧图片{UNREADABLE_MARKER}：{path}（{exc}）") from exc
        if rgba.width > MAX_FRAME_SIZE or rgba.height > MAX_FRAME_SIZE:
            raise FrameLoadError(
                f"帧尺寸 {rgba.width}x{rgba.height} 超过上限 "
                f"{MAX_FRAME_SIZE}x{MAX_FRAME_SIZE}：{path}"
            )
        loaded.append((frame, rgba))

    strip_width = sum(img.width for _, img in loaded)
    strip_height = max(img.height for _, img in loaded)
    strip = Image.new("RGBA", (strip_width, strip_height), (0, 0, 0, 0))

    offset_x = 0
    for _, img in loaded:
        offset_y = (strip_height - img.height) // 2
        # alpha_composite 按源 alpha 混合；条带初始为全透明，
        # 全透明像素写入后 alpha 仍为 0，半透明像素保留原值。
        strip.alpha_composite(img, (offset_x, offset_y))
        offset_x += img.width

    return strip, ordered


def save_strip(strip: Image.Image, output_path: Path) -> None:
    """以无损 PNG 保存条带，保留透明通道。"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    strip.save(output_path, format="PNG", optimize=False)
