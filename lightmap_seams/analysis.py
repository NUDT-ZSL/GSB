"""Per-seam sampling and classification."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable

from PIL import Image

from .seams import Seam

LEAK = "leak"
OUT_OF_BOUNDS = "out_of_bounds"
DEGENERATE = "degenerate"
OK = "ok"

#: Perpendicular distance (in pixels) used for the two side samples.
SAMPLE_OFFSET = 2.0
#: Positions along the seam where both sides are sampled.  Endpoints are
#: excluded so that a corner/cap pixel cannot dominate the result.
SAMPLE_POSITIONS = tuple(0.1 * index for index in range(1, 10))
#: Tolerance used only to keep floating point noise from breaking the
#: "difference equal to threshold is not a leak" boundary rule.
BOUNDARY_EPSILON = 1e-6


@dataclass
class SeamVerdict:
    seam: Seam
    classification: str
    reason: str | None = None
    max_difference: float | None = None
    mean_difference: float | None = None
    max_luminance_a: float | None = None
    max_luminance_b: float | None = None

    def as_dict(self) -> dict:
        data = self.seam.as_dict()
        data["classification"] = self.classification
        data["reason"] = self.reason
        # Numeric evidence is emitted whenever sampling happened, including
        # for seams that stayed below the threshold: it is what makes the
        # boundary rule (difference == threshold is not a leak) auditable.
        if self.max_difference is not None:
            data["max_luminance_a"] = round(self.max_luminance_a or 0.0, 6)
            data["max_luminance_b"] = round(self.max_luminance_b or 0.0, 6)
            data["max_difference"] = round(self.max_difference, 6)
            data["mean_difference"] = round(self.mean_difference or 0.0, 6)
        return data


def luminance(rgb: tuple[int, int, int]) -> float:
    """Rec. 709 relative luminance of an RGB triple (0..255)."""

    red, green, blue = rgb
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def _in_unit_range(value: float) -> bool:
    return 0.0 <= value <= 1.0


def _pixel_coords(seam: Seam, width: int, height: int) -> tuple[float, float, float, float]:
    # UV origin is bottom-left; image row 0 is the top row.
    x1 = seam.u1 * (width - 1)
    y1 = (1.0 - seam.v1) * (height - 1)
    x2 = seam.u2 * (width - 1)
    y2 = (1.0 - seam.v2) * (height - 1)
    return x1, y1, x2, y2


def _out_of_bounds_reason(
    seam: Seam, width: int, height: int
) -> str | None:
    labels = (
        ("u1", seam.u1, "u"),
        ("v1", seam.v1, "v"),
        ("u2", seam.u2, "u"),
        ("v2", seam.v2, "v"),
    )
    for label, value, axis in labels:
        if not _in_unit_range(value):
            return (
                f"坐标越界：端点 {label}={value:g} 的 {axis} 超出 UV[0,1]"
            )
    x1, y1, x2, y2 = _pixel_coords(seam, width, height)
    for label, x, y in (("端点1", x1, y1), ("端点2", x2, y2)):
        if not (0.0 <= x <= width - 1 and 0.0 <= y <= height - 1):
            return f"坐标越界：{label}超出贴图范围 {width}x{height}"
    return None


def _is_degenerate(seam: Seam) -> bool:
    return (
        abs(seam.u1 - seam.u2) <= 1e-9
        and abs(seam.v1 - seam.v2) <= 1e-9
    )


def _sample_pixel(
    pixels, width: int, height: int, x: float, y: float
) -> tuple[int, int, int]:
    """Nearest-pixel sample.

    Callers must have bounds-checked the coordinate; the clamp here only
    guards against a floating point value landing one ULP outside.
    """

    px = min(max(int(round(x)), 0), width - 1)
    py = min(max(int(round(y)), 0), height - 1)
    return pixels[px, py]


def _sample_points(
    seam: Seam, width: int, height: int
) -> list[tuple[float, float, float, float]] | None:
    """Return ``(base_x, base_y, side_a_x, side_a_y)`` sample pairs.

    ``side_b`` is the mirror of ``side_a`` through the base point, so it is
    derived by the caller.  ``None`` means the seam collapsed to zero length
    in pixel space.
    """

    x1, y1, x2, y2 = _pixel_coords(seam, width, height)
    dx = x2 - x1
    dy = y2 - y1
    length = math.hypot(dx, dy)
    if length == 0:
        return None

    normal_x = -dy / length * SAMPLE_OFFSET
    normal_y = dx / length * SAMPLE_OFFSET

    points: list[tuple[float, float, float, float]] = []
    for position in SAMPLE_POSITIONS:
        base_x = x1 + dx * position
        base_y = y1 + dy * position
        points.append((base_x, base_y, base_x + normal_x, base_y + normal_y))
    return points


def classify_seam(seam: Seam, image: Image.Image, threshold: int) -> SeamVerdict:
    """Classify one seam against a decoded RGB image."""

    width, height = image.size

    bounds_reason = _out_of_bounds_reason(seam, width, height)
    if bounds_reason:
        return SeamVerdict(seam=seam, classification=OUT_OF_BOUNDS, reason=bounds_reason)

    if _is_degenerate(seam):
        return SeamVerdict(
            seam=seam,
            classification=DEGENERATE,
            reason="退化接缝：两端点 UV 相同，不参与漏光判定",
        )

    points = _sample_points(seam, width, height)
    if points is None:
        return SeamVerdict(
            seam=seam,
            classification=DEGENERATE,
            reason="退化接缝：两端点在贴图上重合，不参与漏光判定",
        )

    for base_x, base_y, side_x, side_y in points:
        mirror_x = 2.0 * base_x - side_x
        mirror_y = 2.0 * base_y - side_y
        for x, y in ((side_x, side_y), (mirror_x, mirror_y)):
            if not (0.0 <= x <= width - 1 and 0.0 <= y <= height - 1):
                return SeamVerdict(
                    seam=seam,
                    classification=OUT_OF_BOUNDS,
                    reason=(
                        f"坐标越界：采样点 ({x:.1f}, {y:.1f}) 落在贴图范围外"
                        f"（{width}x{height}）"
                    ),
                )

    pixels = image.load()
    per_sample: list[tuple[float, float]] = []
    for base_x, base_y, side_x, side_y in points:
        mirror_x = 2.0 * base_x - side_x
        mirror_y = 2.0 * base_y - side_y
        lum_a = luminance(_sample_pixel(pixels, width, height, side_x, side_y))
        lum_b = luminance(_sample_pixel(pixels, width, height, mirror_x, mirror_y))
        per_sample.append((lum_a, lum_b))

    if not per_sample:
        return SeamVerdict(
            seam=seam,
            classification=OK,
            reason="无有效采样点",
            max_difference=0.0,
            mean_difference=0.0,
        )

    differences = [abs(a - b) for a, b in per_sample]
    max_index = max(range(len(differences)), key=differences.__getitem__)
    max_difference = differences[max_index]
    mean_difference = sum(differences) / len(differences)
    lum_a, lum_b = per_sample[max_index]

    is_leak = max_difference > threshold and not math.isclose(
        max_difference, float(threshold), abs_tol=BOUNDARY_EPSILON
    )

    if is_leak:
        reason = (
            f"漏光候选：两侧亮度差 {max_difference:.2f} 超过阈值 {threshold}"
        )
        return SeamVerdict(
            seam=seam,
            classification=LEAK,
            reason=reason,
            max_difference=max_difference,
            mean_difference=mean_difference,
            max_luminance_a=lum_a,
            max_luminance_b=lum_b,
        )

    return SeamVerdict(
        seam=seam,
        classification=OK,
        reason=f"亮度差 {max_difference:.2f} 未超过阈值 {threshold}",
        max_difference=max_difference,
        mean_difference=mean_difference,
        max_luminance_a=lum_a,
        max_luminance_b=lum_b,
    )


def classify_seams(
    seams: Iterable[Seam], image: Image.Image, threshold: int
) -> list[SeamVerdict]:
    return [classify_seam(seam, image, threshold) for seam in seams]
