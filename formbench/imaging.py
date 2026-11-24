from __future__ import annotations

from pathlib import Path

import numpy as np

from .errors import ImageValidationError


def read_pgm(path: str | Path) -> np.ndarray:
    """Read an 8-bit binary/ASCII PGM as a uint8 grayscale array."""
    path = Path(path)
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ImageValidationError(
            f"cannot read image: {exc}", field=str(path), reason_code="E_IMAGE_UNREADABLE"
        ) from exc

    pos = 0

    def token() -> bytes:
        nonlocal pos
        while True:
            while pos < len(data) and data[pos] in b" \t\r\n":
                pos += 1
            if pos >= len(data):
                raise ImageValidationError("truncated PGM header", field=str(path))
            if data[pos : pos + 1] == b"#":
                while pos < len(data) and data[pos] not in b"\r\n":
                    pos += 1
                continue
            start = pos
            while pos < len(data) and data[pos] not in b" \t\r\n":
                pos += 1
            return data[start:pos]

    magic = token()
    if magic not in (b"P5", b"P2"):
        raise ImageValidationError(
            f"unsupported image magic {magic!r}; expected P5/P2 PGM",
            field=str(path),
            reason_code="E_IMAGE_FORMAT",
        )
    try:
        width = int(token())
        height = int(token())
        maxval = int(token())
    except ValueError as exc:
        raise ImageValidationError("invalid PGM header integer", field=str(path)) from exc

    if width <= 0 or height <= 0 or maxval != 255:
        raise ImageValidationError(
            "PGM must have positive dimensions and maxval=255", field=str(path)
        )

    if magic == b"P5":
        while pos < len(data) and data[pos] in b" \t\r\n":
            pos += 1
        expected = width * height
        pixels = data[pos : pos + expected]
        if len(pixels) != expected:
            raise ImageValidationError(
                f"image pixel data is truncated: expected {expected}, got {len(pixels)}",
                field=str(path),
                reason_code="E_IMAGE_TRUNCATED",
            )
        arr = np.frombuffer(pixels, dtype=np.uint8).reshape(height, width).copy()
    else:
        try:
            values = [int(x) for x in data[pos:].split()]
        except ValueError as exc:
            raise ImageValidationError("invalid ASCII PGM pixels", field=str(path)) from exc
        if len(values) != width * height:
            raise ImageValidationError("ASCII PGM has wrong pixel count", field=str(path))
        arr = np.asarray(values, dtype=np.uint8).reshape(height, width)
    return arr


def write_pgm(path: str | Path, image: np.ndarray) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if image.dtype != np.uint8 or image.ndim != 2:
        raise ValueError("write_pgm expects a 2-D uint8 array")
    header = f"P5\n{image.shape[1]} {image.shape[0]}\n255\n".encode("ascii")
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("wb") as handle:
        handle.write(header)
        handle.write(image.tobytes())
        handle.flush()
    tmp.replace(path)


def ink_mask(image: np.ndarray, cutoff: int = 170) -> np.ndarray:
    return image < cutoff


def draw_line(
    canvas: np.ndarray,
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    *,
    value: int = 20,
    width: float = 2.0,
) -> None:
    """Draw an anti-aliased-ish thick line directly on a grayscale canvas."""
    height, width_px = canvas.shape
    margin = int(np.ceil(width + 2))
    min_x = max(0, int(np.floor(min(x0, x1))) - margin)
    max_x = min(width_px - 1, int(np.ceil(max(x0, x1))) + margin)
    min_y = max(0, int(np.floor(min(y0, y1))) - margin)
    max_y = min(height - 1, int(np.ceil(max(y0, y1))) + margin)
    if min_x > max_x or min_y > max_y:
        return

    yy, xx = np.mgrid[min_y : max_y + 1, min_x : max_x + 1]
    dx = x1 - x0
    dy = y1 - y0
    length2 = dx * dx + dy * dy
    if length2 == 0:
        distance = np.hypot(xx - x0, yy - y0)
    else:
        t = np.clip(((xx - x0) * dx + (yy - y0) * dy) / length2, 0.0, 1.0)
        px = x0 + t * dx
        py = y0 + t * dy
        distance = np.hypot(xx - px, yy - py)
    coverage = np.clip(width / 2.0 + 0.5 - distance, 0.0, 1.0)
    region = canvas[min_y : max_y + 1, min_x : max_x + 1]
    ink = np.asarray(value, dtype=np.float32)
    region[:] = (region.astype(np.float32) * (1.0 - coverage) + ink * coverage).astype(np.uint8)


def rotate_grayscale(image: np.ndarray, angle_degrees: float, fill: int = 245) -> np.ndarray:
    """Rotate around the center with inverse nearest sampling; NumPy only."""
    if abs(angle_degrees) < 1e-12:
        return image.copy()
    h, w = image.shape
    cy = (h - 1) / 2.0
    cx = (w - 1) / 2.0
    theta = np.deg2rad(angle_degrees)
    cosine = np.cos(theta)
    sine = np.sin(theta)
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    dx = xx - cx
    dy = yy - cy
    src_x = np.rint(cx + cosine * dx + sine * dy).astype(np.int64)
    src_y = np.rint(cy - sine * dx + cosine * dy).astype(np.int64)
    valid = (src_x >= 0) & (src_x < w) & (src_y >= 0) & (src_y < h)
    output = np.full_like(image, fill)
    output[valid] = image[src_y[valid], src_x[valid]]
    return output


def bbox_of_mask(mask: np.ndarray) -> tuple[int, int, int, int] | None:
    ys, xs = np.where(mask)
    if xs.size == 0:
        return None
    x0 = int(xs.min())
    y0 = int(ys.min())
    x1 = int(xs.max()) + 1
    y1 = int(ys.max()) + 1
    return x0, y0, x1 - x0, y1 - y0
