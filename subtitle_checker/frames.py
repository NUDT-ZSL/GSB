"""Frame lookup and decoding.

Frames are matched to subtitle ids by file name: ``<id>.png`` (with common image
extensions tried in a fixed order). A frame that is missing or cannot be fully
decoded makes its subtitle *unverified*; every other subtitle is still checked.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image, UnidentifiedImageError

FRAME_EXTENSIONS = (".png", ".jpg", ".jpeg", ".bmp", ".webp")
MAX_FRAME_PIXELS = 100_000_000


@dataclass(frozen=True)
class FrameInfo:
    name: str | None
    status: str  # "ok" | "missing" | "decode_error"
    reason: str | None
    width: int | None
    height: int | None
    path: Path | None

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "status": self.status,
            "reason": self.reason,
            "width": self.width,
            "height": self.height,
        }


def locate_frame(frames_dir: Path, subtitle_id: str) -> Path | None:
    for ext in FRAME_EXTENSIONS:
        candidate = frames_dir / f"{subtitle_id}{ext}"
        if candidate.is_file():
            return candidate
    return None


def _safe_exception_text(exc: BaseException, path: Path) -> str:
    message = str(exc)
    for raw in (str(path.resolve()), str(path)):
        message = message.replace(raw, path.name)
    return f"{type(exc).__name__}: {message}".strip()


def inspect_frame(frames_dir: Path, subtitle_id: str) -> FrameInfo:
    path = locate_frame(frames_dir, subtitle_id)
    if path is None:
        tried = "/".join(FRAME_EXTENSIONS)
        return FrameInfo(
            name=f"{subtitle_id}.{FRAME_EXTENSIONS[0][1:]}",
            status="missing",
            reason=f"帧图缺失：帧图目录中未找到 {subtitle_id}{FRAME_EXTENSIONS[0]}（已尝试扩展名 {tried}）",
            width=None,
            height=None,
            path=None,
        )

    try:
        with Image.open(path) as img:
            width, height = img.size
            if width <= 0 or height <= 0:
                raise ValueError(f"帧图尺寸非法 {width}x{height}")
            if width * height > MAX_FRAME_PIXELS:
                raise ValueError(f"帧图像素总数过大（{width}x{height}）")
            img.load()
    except UnidentifiedImageError as exc:
        return FrameInfo(path.name, "decode_error", f"帧图无法解码：{path.name}（{type(exc).__name__}）", None, None, path)
    except (OSError, SyntaxError, ValueError, Image.DecompressionBombError) as exc:
        return FrameInfo(
            path.name,
            "decode_error",
            f"帧图无法解码：{path.name}（{_safe_exception_text(exc, path)}）",
            None,
            None,
            path,
        )

    return FrameInfo(path.name, "ok", None, width, height, path)
