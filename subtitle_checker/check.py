"""Per-subtitle checking logic and timeline overlap detection.

Group assignment precedence (first match wins):

1. ``timing``     -- start/end inverted or zero duration; skipped, never rendered.
2. ``unverified`` -- the frame image is missing or cannot be decoded. Such an entry
   cannot be signed off, so it is reported as unverified even if geometry also has
   findings; those findings are still listed in ``reasons``.
3. ``overflow``   -- the text block leaves the safe area.
4. ``occlusion``  -- the text block overlaps the FX region.
5. ``pass``       -- all checks passed.

Groups are *displayed* in severity order overflow -> occlusion -> timing ->
unverified -> pass (``GROUP_ORDER``). Every entry lands in exactly one group, and
entries carry the lower-priority findings in their detail record (for example an
overflowing entry that is also occluded reports the occlusion ratio), so no
finding is lost.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .frames import FrameInfo, inspect_frame
from .geometry import (
    Box,
    Overflow,
    TextMetrics,
    intersection_area,
    measure_text,
    overflow_against,
    safe_area,
    text_box,
)
from .manifest import Manifest, Subtitle

GROUP_ORDER = ("overflow", "occlusion", "timing", "unverified", "pass")
GROUP_TITLES = {
    "overflow": "越界",
    "occlusion": "遮挡",
    "timing": "时间异常",
    "unverified": "未核对",
    "pass": "通过",
}
EDGE_TITLES = {"top": "顶部越界", "right": "右侧越界", "bottom": "底部越界", "left": "左侧越界"}


@dataclass
class SubtitleResult:
    subtitle: Subtitle
    group: str
    reasons: list[str] = field(default_factory=list)
    metrics: TextMetrics | None = None
    box: Box | None = None
    overflow: Overflow | None = None
    occlusion_ratio: float = 0.0
    occlusion_area: int = 0
    frame: FrameInfo | None = None
    render_skipped: bool = False

    @property
    def id(self) -> str:
        return self.subtitle.id

    def as_dict(self) -> dict[str, object]:
        data: dict[str, object] = {
            "id": self.subtitle.id,
            "group": self.group,
            "group_title": GROUP_TITLES[self.group],
            "start_ms": self.subtitle.start_ms,
            "end_ms": self.subtitle.end_ms,
            "duration_ms": self.subtitle.end_ms - self.subtitle.start_ms,
            "font_size": self.subtitle.font_size,
            "anchor": {"x": self.subtitle.anchor_x, "y": self.subtitle.anchor_y},
            "text": self.subtitle.text,
            "reasons": list(self.reasons),
        }
        if self.metrics is not None:
            data["text_metrics"] = self.metrics.as_dict()
        if self.box is not None:
            data["text_box"] = self.box.as_dict()
        if self.overflow is not None and self.overflow.triggered:
            data["overflow"] = {
                "directions": self.overflow.directions(),
                "pixels": self.overflow.as_dict(),
                "total_px": self.overflow.total,
                "detail": overflow_detail(self.overflow),
            }
        if self.occlusion_area > 0 and self.box is not None:
            data["occlusion"] = {
                "overlap_area_px2": self.occlusion_area,
                "text_area_px2": self.box.area,
                "ratio": round(self.occlusion_ratio, 6),
                "percent": round(self.occlusion_ratio * 100.0, 2),
            }
        if self.frame is not None:
            data["frame"] = self.frame.as_dict()
        if self.render_skipped:
            data["render_skipped"] = True
        return data


def overflow_detail(overflow: Overflow) -> str:
    parts = []
    for direction in ("top", "right", "bottom", "left"):
        amount = getattr(overflow, direction)
        if amount > 0:
            parts.append(f"{EDGE_TITLES[direction]} {amount} px")
    return "、".join(parts) if parts else "无越界"


def timing_problem(sub: Subtitle) -> str | None:
    if sub.end_ms < sub.start_ms:
        return f"起止倒置：start_ms={sub.start_ms} 晚于 end_ms={sub.end_ms}（时长 {sub.end_ms - sub.start_ms} ms）"
    if sub.end_ms == sub.start_ms:
        return f"时长为零：start_ms=end_ms={sub.start_ms} ms"
    return None


def _fx_box(manifest: Manifest) -> Box:
    rect = manifest.fx_region
    return Box(left=rect.x, top=rect.y, right=rect.x + rect.width, bottom=rect.y + rect.height)


def analyse_subtitle(sub: Subtitle, manifest: Manifest, frames_dir: Path) -> SubtitleResult:
    problem = timing_problem(sub)
    if problem is not None:
        # Skipped entirely: no geometry, no frame inspection, no preview.
        return SubtitleResult(
            subtitle=sub,
            group="timing",
            reasons=[problem, "该条目已跳过：不参与渲染、安全区与遮挡判定"],
            render_skipped=True,
        )

    metrics = measure_text(sub.text, sub.font_size)
    box = text_box(sub.anchor_x, sub.anchor_y, metrics)
    safe = safe_area(manifest.canvas.width, manifest.canvas.height, manifest.margin)
    overflow = overflow_against(box, safe)
    fx = _fx_box(manifest)
    occluded_area = intersection_area(box, fx)
    ratio = (occluded_area / box.area) if box.area > 0 else 0.0

    frame = inspect_frame(frames_dir, sub.id)

    geometry_reasons: list[str] = []
    if overflow.triggered:
        geometry_reasons.append(f"安全区越界：{overflow_detail(overflow)}（合计 {overflow.total} px）")
    if occluded_area > 0:
        geometry_reasons.append(
            f"特效区遮挡：遮挡面积 {occluded_area} px²，占文本面积 {ratio * 100:.2f}%（比例 {ratio:.4f}）"
        )

    reasons: list[str] = []
    if not frame.ok:
        # Requirement 10: a missing/undecodable frame makes the entry unverified.
        group = "unverified"
        reasons.append(frame.reason or "帧图不可用，未完成核对")
        if geometry_reasons:
            reasons.append("几何检查结果（仅供参考，待帧图补齐后确认）：" + "；".join(geometry_reasons))
    elif overflow.triggered:
        group = "overflow"
        reasons.extend(geometry_reasons)
    elif occluded_area > 0:
        group = "occlusion"
        reasons.extend(geometry_reasons)
    else:
        group = "pass"
        reasons.append("安全区与特效区检查均通过")

    return SubtitleResult(
        subtitle=sub,
        group=group,
        reasons=reasons,
        metrics=metrics,
        box=box,
        overflow=overflow,
        occlusion_ratio=ratio,
        occlusion_area=occluded_area,
        frame=frame,
    )


@dataclass(frozen=True)
class OverlapPair:
    first_id: str
    second_id: str
    overlap_ms: int

    def as_dict(self) -> dict[str, object]:
        return {"ids": [self.first_id, self.second_id], "overlap_ms": self.overlap_ms}


def find_overlaps(subtitles: Iterable[Subtitle]) -> list[OverlapPair]:
    """List every pair of renderable subtitles whose time intervals overlap.

    Timing anomalies are excluded because they are never rendered. Pairs are
    sorted by id, ascending (plain lexicographic order).
    """
    valid = [s for s in subtitles if timing_problem(s) is None]
    pairs: list[OverlapPair] = []
    for i, a in enumerate(valid):
        for b in valid[i + 1 :]:
            overlap = min(a.end_ms, b.end_ms) - max(a.start_ms, b.start_ms)
            if overlap > 0:
                first, second = sorted((a.id, b.id))
                pairs.append(OverlapPair(first, second, overlap))
    pairs.sort(key=lambda p: (p.first_id, p.second_id))
    return pairs


def sort_by_id(results: Iterable[SubtitleResult]) -> list[SubtitleResult]:
    """Ascending id order (plain lexicographic, stable for equal ids)."""
    return sorted(results, key=lambda r: r.id)
