"""Per-frame compositing: decode, normalise, resize, centre on the canvas.

Rules implemented here:
  * The frame is placed on an RGBA canvas filled with the configured
    background colour.  The canvas is *real pixels*, not CSS padding.
  * Frames smaller than the canvas are centred; frames larger than the canvas
    are scaled down (aspect ratio preserved, no cropping).  A frame that
    nearly fits is only downscaled when that is the only way to keep all
    visible content.
  * Missing alpha is treated as fully opaque and reported separately.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field

from PIL import Image, ImageChops, ImageOps, UnidentifiedImageError

from .errors import FrameFailure
from .spec import CanvasConfig, Background

# Pillow's own decompression-bomb guard defaults are fine for storyboard frames,
# but a very large PNG may hit them.  Storyboard frames are legitimately large,
# so raise the limit and keep the hard failure explicit instead of silently
# trusting attacker-controlled dimensions.
Image.MAX_IMAGE_PIXELS = 200_000_000

_RESAMPLING = {
    "nearest": Image.Resampling.NEAREST,
    "bilinear": Image.Resampling.BILINEAR,
    "bicubic": Image.Resampling.BICUBIC,
    "lanczos": Image.Resampling.LANCZOS,
}
DEFAULT_RESAMPLING = "lanczos"


@dataclass
class Orientation:
    mode: str  # exif_transposed | already_upright | not_needed
    exif_orientation: int | None = None

    @property
    def exif_applied(self) -> bool:
        return self.mode == "exif_transposed"


@dataclass
class Composited:
    image: Image.Image
    source_width: int
    source_height: int
    source_mode: str
    source_format: str | None
    has_alpha: bool
    alpha_min: int
    alpha_max: int
    placement: str  # as_is | centered | downscaled
    scale: float  # 1.0 when no scaling
    content_box: tuple[int, int, int, int]
    orientation: Orientation
    had_alpha_fully_opaque: bool = False
    notes: list[str] = field(default_factory=list)


def _decode(data: bytes, source_name: str) -> tuple[Image.Image, str | None]:
    if not data:
        raise FrameFailure("frame_empty", "帧文件为空（0 字节）")
    try:
        with Image.open(io.BytesIO(data), formats=None) as probe:
            probe_format = probe.format
            probe.verify()
        # verify() invalidates the object; reopen for the real decode.
        image = Image.open(io.BytesIO(data))
        image.load()
    except UnidentifiedImageError:
        raise FrameFailure(
            "frame_undecodable",
            "帧文件无法识别为受支持的图片格式（可能已损坏）",
        ) from None
    except Image.UnidentifiedImageError:  # pragma: no cover - alias guard
        raise FrameFailure("frame_undecodable", "帧文件无法识别为受支持的图片格式") from None
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError) as exc:
        raise FrameFailure(
            "frame_undecodable",
            f"帧文件解码失败：{type(exc).__name__}: {exc}",
        ) from None
    except Exception as exc:  # noqa: BLE001 - Pillow raises many plugin-specific errors
        raise FrameFailure(
            "frame_undecodable",
            f"帧文件解码失败：{type(exc).__name__}: {exc}",
        ) from None

    return image, probe_format


def _transparency_key(image: Image.Image) -> tuple[int, int, int] | None:
    key = image.info.get("transparency")
    if isinstance(key, int) and image.mode == "L":
        return (key, key, key)
    if isinstance(key, int) and image.mode == "RGB":
        return ((key >> 16) & 0xFF, (key >> 8) & 0xFF, key & 0xFF)
    if isinstance(key, (tuple, list)) and len(key) >= 3:
        return tuple(int(part) for part in key[:3])  # type: ignore[return-value]
    return None


def _to_rgba(image: Image.Image) -> tuple[Image.Image, bool, int, int]:
    """Return (RGBA copy, original had alpha, alpha min, alpha max)."""
    has_alpha = False
    alpha_min = alpha_max = 255
    if image.mode in ("RGBA", "LA", "PA", "RGBa", "La") or (
        image.mode == "P" and "transparency" in image.info
    ):
        converted = image.convert("RGBA")
        alpha = converted.getchannel("A")
        alpha_min, alpha_max = alpha.getextrema()
        # An alpha channel that is entirely opaque still counts as present.
        has_alpha = True
    elif image.mode in ("RGB", "L") and "transparency" in image.info:
        # PNG tRNS colour key: one colour is fully transparent.  Build a
        # binary alpha mask without a per-pixel Python loop.
        converted = image.convert("RGBA")
        target = _transparency_key(image)
        if target is not None:
            solid = Image.new("RGB", image.size, target)
            diff = ImageChops.difference(image.convert("RGB"), solid).convert("L")
            mask = diff.point(lambda value: 255 if value else 0)
            converted.putalpha(mask)
            alpha_min, alpha_max = converted.getchannel("A").getextrema()
            has_alpha = True
    elif image.mode == "CMYK":
        converted = image.convert("RGBA")
    elif image.mode == "1":
        converted = image.convert("RGBA")
    elif image.mode == "I;16":
        converted = image.convert("I").convert("RGB").convert("RGBA")
    else:
        try:
            converted = image.convert("RGBA")
        except ValueError:
            raise FrameFailure(
                "frame_unsupported_mode",
                f"不支持的像素模式：{image.mode}",
            ) from None
    return converted, has_alpha, alpha_min, alpha_max


def _apply_exif_orientation(
    image: Image.Image,
) -> tuple[Image.Image, Orientation]:
    try:
        exif = image.getexif()
    except Exception:  # noqa: BLE001 - malformed EXIF must not kill the batch
        exif = None
    orientation = None
    if exif:
        try:
            orientation = exif.get(0x0112)
        except Exception:  # noqa: BLE001
            orientation = None
    if isinstance(orientation, int) and orientation not in (0, 1):
        transposed = ImageOps.exif_transpose(image)
        if transposed is not None:
            return transposed, Orientation("exif_transposed", orientation)
        return image, Orientation("already_upright", orientation)
    return image, Orientation("not_needed", orientation if isinstance(orientation, int) else None)


def _scaled_size(width: int, height: int, max_w: int, max_h: int) -> tuple[int, int]:
    """Uniform scale that fits inside the canvas; floor keeps it from overflowing."""
    scale = min(max_w / width, max_h / height)
    new_w = max(1, int(width * scale))
    new_h = max(1, int(height * scale))
    return min(new_w, max_w), min(new_h, max_h)


def resize_within(
    image: Image.Image,
    width: int,
    height: int,
    resampling: str,
) -> Image.Image:
    if image.width == width and image.height == height:
        return image
    return image.resize((width, height), _RESAMPLING[resampling])


def validate_resampling(name: str) -> str:
    normalised = name.strip().lower()
    if normalised not in _RESAMPLING:
        allowed = ", ".join(sorted(_RESAMPLING))
        raise FrameFailure(
            "invalid_resampling",
            f"未知缩放算法 {name!r}，可选：{allowed}",
        )
    return normalised


def composite(
    data: bytes,
    source_name: str,
    canvas: CanvasConfig,
    background: Background,
    resampling: str = DEFAULT_RESAMPLING,
) -> Composited:
    image, source_format = _decode(data, source_name)
    notes: list[str] = []

    image, orientation = _apply_exif_orientation(image)
    if orientation.exif_applied:
        notes.append(
            f"已按 EXIF Orientation={orientation.exif_orientation} 旋正"
        )

    source_width, source_height = image.width, image.height
    source_mode = image.mode
    rgba, has_alpha, alpha_min, alpha_max = _to_rgba(image)

    content = rgba
    placement = "as_is"
    scale = 1.0
    if source_width > canvas.width or source_height > canvas.height:
        new_w, new_h = _scaled_size(
            source_width, source_height, canvas.width, canvas.height
        )
        content = resize_within(rgba, new_w, new_h, resampling)
        scale = new_w / source_width
        placement = "downscaled"
        notes.append(
            f"源图 {source_width}x{source_height} 超出画幅，"
            f"等比缩放至 {new_w}x{new_h}（比例 {scale:.4f}）"
        )
    elif source_width < canvas.width or source_height < canvas.height:
        placement = "centered"
        left = (canvas.width - source_width) // 2
        top = (canvas.height - source_height) // 2
        right = canvas.width - source_width - left
        bottom = canvas.height - source_height - top
        notes.append(
            f"源图 {source_width}x{source_height} 小于画幅，"
            f"居中留白 左{left} 上{top} 右{right} 下{bottom} 像素"
        )

    canvas_image = Image.new("RGBA", (canvas.width, canvas.height), background.rgba)
    left = (canvas.width - content.width) // 2
    top = (canvas.height - content.height) // 2
    # RGBA paste replaces pixels including alpha, so a transparent source
    # region shows the background rather than stale canvas pixels.
    canvas_image.paste(content, (left, top))

    return Composited(
        image=canvas_image,
        source_width=source_width,
        source_height=source_height,
        source_mode=source_mode,
        source_format=source_format,
        has_alpha=has_alpha,
        alpha_min=alpha_min,
        alpha_max=alpha_max,
        placement=placement,
        scale=scale,
        content_box=(left, top, left + content.width, top + content.height),
        orientation=orientation,
        had_alpha_fully_opaque=has_alpha and alpha_min >= 255,
        notes=notes,
    )
