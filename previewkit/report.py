"""生成结果报告（JSON）。

报告是唯一的交付契约：完整镜头列表、已合成字幕条数、未完成部分明细（按原因
分类）都在这里，分类字段取值固定（见 subtitles.REASON_ORDER）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import __version__
from .models import (
    ComposedSubtitle,
    FrameSpec,
    Gap,
    PlacedShot,
    Shot,
    UnfinishedItem,
)
from .planner import KIND_GAP
from .subtitles import REASON_LABELS, REASON_ORDER
from .timeline import canonical_order

SCHEMA_VERSION = "1.0"

# 报告里 video_lossless 的判定依据，与 video.pick_codec 的可选编码器保持同步
LOSSLESS_CODECS = {"ffv1", "libx264rgb", "huffyuv", "ffvhuff"}


def _shot_entry(shot: Shot) -> dict[str, Any]:
    return {
        "shot_id": shot.shot_id,
        "start_ms": shot.start_ms,
        "end_ms": shot.end_ms,
        "input_index": shot.input_index,
    }


def _placed_entry(placed: PlacedShot) -> dict[str, Any]:
    return {
        "shot_id": placed.shot.shot_id,
        "start_ms": placed.shot.start_ms,
        "end_ms": placed.shot.end_ms,
        "effective_start_ms": placed.effective_start_ms,
        "effective_end_ms": placed.effective_end_ms,
        "status": placed.status,
        "rendered": placed.effective_end_ms > placed.effective_start_ms,
    }


def build_report(
    *,
    shots: list[Shot],
    placed: list[PlacedShot],
    gaps: list[Gap],
    conflicts,
    composed: list[ComposedSubtitle],
    unfinished: list[UnfinishedItem],
    frames: list[FrameSpec],
    fps: int,
    width: int,
    height: int,
    total_ms: int,
    video_path: Path,
    report_path: Path,
    shots_source: str,
    subtitles_source: str,
    codec: str,
    include_frame_index: bool,
    verification: dict[str, Any] | None,
) -> dict[str, Any]:
    by_id = {p.shot.shot_id: p for p in placed}
    ordered = canonical_order(shots)

    frame_ranges: dict[str, list[int]] = {}
    for spec in frames:
        if spec.shot_id is not None:
            frame_ranges.setdefault(spec.shot_id, [spec.index, spec.index])[1] = spec.index

    shot_entries: list[dict[str, Any]] = []
    for shot in ordered:
        entry = _shot_entry(shot)
        placement = by_id[shot.shot_id]
        entry.update(
            {
                "effective_start_ms": placement.effective_start_ms,
                "effective_end_ms": placement.effective_end_ms,
                "status": placement.status,
                "rendered": placement.effective_end_ms > placement.effective_start_ms,
                "frame_range": frame_ranges.get(shot.shot_id),
                "composed_subtitles": [
                    c.subtitle.index for c in composed if shot.shot_id in c.shot_ids
                ],
            }
        )
        shot_entries.append(entry)

    reason_groups = []
    for reason in REASON_ORDER:
        items = [u for u in unfinished if u.reason == reason]
        if not items:
            continue
        reason_groups.append(
            {
                "reason": reason,
                "label": REASON_LABELS[reason],
                "count": len(items),
                "items": [
                    {
                        "subtitle_index": u.subtitle.index,
                        "text": u.subtitle.text,
                        "start_ms": u.subtitle.start_ms,
                        "end_ms": u.subtitle.end_ms,
                        "shot_id": u.subtitle.shot_id,
                        "detail": u.detail,
                    }
                    for u in items
                ],
            }
        )

    gap_frame_count = sum(1 for f in frames if f.kind == KIND_GAP)
    complete = not unfinished and not conflicts and not gaps

    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generator": f"previewkit {__version__}",
        "status": "complete" if complete else "partial",
        "inputs": {
            "shots": shots_source,
            "subtitles": subtitles_source,
            "shots_count": len(shots),
            "subtitles_count": len(composed) + len(unfinished),
        },
        "settings": {
            "width": width,
            "height": height,
            "fps": fps,
            "duration_ms": total_ms,
            "duration_s": round(total_ms / 1000, 3),
            "frame_count": len(frames),
            "codec": codec,
            "video_lossless": codec in LOSSLESS_CODECS,
        },
        "outputs": {
            "video": video_path.name,
            "report": report_path.name,
        },
        "summary": {
            "shots_total": len(shots),
            "shots_rendered": sum(1 for p in placed if p.effective_end_ms > p.effective_start_ms),
            "shots_fully_overlapped": sum(
                1 for p in placed if p.effective_end_ms <= p.effective_start_ms
            ),
            "subtitles_total": len(composed) + len(unfinished),
            "subtitles_composed": len(composed),
            "subtitles_unfinished": len(unfinished),
            "subtitles_spanning_cuts": sum(1 for c in composed if len(c.shot_ids) > 1),
            "conflicts": len(conflicts),
            "gaps": len(gaps),
            "gap_frames": gap_frame_count,
        },
        "shots": {
            "canonical": shot_entries,
            "order_note": "canonical 按 (start_ms, end_ms, input_index) 升序；input_index 为原始位置",
            "render_segments": [
                _placed_entry(p) for p in placed if p.effective_end_ms > p.effective_start_ms
            ],
            "fully_overlapped": [
                p.shot.shot_id for p in placed if p.effective_end_ms <= p.effective_start_ms
            ],
        },
        "conflicts": [
            {
                "earlier_shot_id": c.earlier.shot_id,
                "earlier_start_ms": c.earlier.start_ms,
                "earlier_end_ms": c.earlier.end_ms,
                "later_shot_id": c.later.shot_id,
                "later_start_ms": c.later.start_ms,
                "later_end_ms": c.later.end_ms,
                "overlap_start_ms": c.overlap_start_ms,
                "overlap_end_ms": c.overlap_end_ms,
                "cut_at_ms": c.earlier.end_ms,
                "note": "以更早镜头的结束时刻切分，后一镜头的头部不参与渲染",
            }
            for c in conflicts
        ],
        "gaps": [
            {
                "start_ms": g.start_ms,
                "end_ms": g.end_ms,
                "duration_ms": g.duration_ms,
                "note": "空档渲染为空画面占位，不延续相邻镜头，也不显示字幕",
            }
            for g in gaps
        ],
        "subtitles": {
            "composed_count": len(composed),
            "composed": [
                {
                    "subtitle_index": c.subtitle.index,
                    "text": c.subtitle.text,
                    "start_ms": c.subtitle.start_ms,
                    "end_ms": c.subtitle.end_ms,
                    "shot_id": c.subtitle.shot_id,
                    "covered_shots": list(c.shot_ids),
                    "spans_cut": len(c.shot_ids) > 1,
                }
                for c in sorted(composed, key=lambda c: c.subtitle.index)
            ],
            "unfinished_count": len(unfinished),
            "unfinished": [
                {
                    "subtitle_index": u.subtitle.index,
                    "text": u.subtitle.text,
                    "start_ms": u.subtitle.start_ms,
                    "end_ms": u.subtitle.end_ms,
                    "shot_id": u.subtitle.shot_id,
                    "reason": u.reason,
                    "detail": u.detail,
                }
                for u in unfinished
            ],
            "unfinished_by_reason": reason_groups,
        },
    }
    if include_frame_index:
        report["frame_index"] = [
            {
                "index": f.index,
                "time_ms": f.time_ms,
                "kind": f.kind,
                "shot_id": f.shot_id,
                "subtitle_texts": list(f.subtitle_texts),
            }
            for f in frames
        ]
    if verification is not None:
        report["verification"] = verification
    return report


def write_report(report: dict[str, Any], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.stem + ".part" + path.suffix)
    text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    tmp.write_text(text, encoding="utf-8", newline="\n")
    tmp.replace(path)
    return path
