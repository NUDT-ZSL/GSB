"""节奏核查结果 JSON 输出（需求 8、10）。

结果按镜头清单顺序组织，并显式区分两个区域：

* ``completed``：已完成对齐的镜头，字段完整可逐条核算；
* ``incomplete``：因时长缺失 / 为零等原因未完成的镜头，只记录原因，
  绝不以对齐成功的形态出现（需求 10）。
"""

from __future__ import annotations

import json
import os

from .align import DIRECTION_LABELS, AlignedShot
from .audio import AudioInfo
from .detect import Beat
from .preview import format_timecode
from .shots import ShotEntry, STATUS_ALIGNED, STATUS_SKIPPED

SCHEMA_VERSION = "1.0"


def _aligned_item(shot: AlignedShot) -> dict:
    return {
        "index": shot.entry.index,
        "title": shot.entry.title,
        "status": STATUS_ALIGNED,
        "duration_ms": shot.entry.duration_ms,
        "original_start_ms": shot.original_start_ms,
        "aligned_start_ms": shot.aligned_start_ms,
        "offset_ms": shot.offset_ms,
        "signed_offset_ms": shot.signed_offset_ms,
        "direction": shot.direction,
        "direction_label": DIRECTION_LABELS[shot.direction],
        "beat_time_ms": shot.beat_time_ms,
        "aligned_timecode": format_timecode(shot.aligned_start_ms),
        "original_timecode": format_timecode(shot.original_start_ms),
        "preview_image": shot.entry.extras.get("_preview_path", ""),
    }


def _skipped_item(entry: ShotEntry) -> dict:
    return {
        "index": entry.index,
        "title": entry.title,
        "status": STATUS_SKIPPED,
        "duration_ms": entry.duration_ms,
        "original_start_ms": entry.original_start_ms,
        "reason": entry.skip_reason,
        "original_timecode": format_timecode(entry.original_start_ms),
    }


def build_report(
    audio: AudioInfo,
    beats: list[Beat],
    entries: list[ShotEntry],
    aligned_shots: list[AlignedShot],
    overhang: dict,
    detector_params: dict,
    preview_paths: list[str] | None,
) -> dict:
    """构造可直接 JSON 序列化的核查结果字典（顺序与镜头清单一致）。"""
    aligned_by_index = {shot.entry.index: shot for shot in aligned_shots}
    if preview_paths:
        # 把预览路径回填到对应镜头，保持与 aligned_shots 同序
        for shot, path in zip(aligned_shots, preview_paths):
            shot.entry.extras["_preview_path"] = path

    completed: list[dict] = []
    incomplete: list[dict] = []
    for entry in entries:  # 遍历源清单，天然保持原顺序
        if entry.skipped:
            incomplete.append(_skipped_item(entry))
        else:
            shot = aligned_by_index[entry.index]
            completed.append(_aligned_item(shot))

    return {
        "schema_version": SCHEMA_VERSION,
        "audio": {
            "path": os.path.abspath(audio.path),
            "sample_rate": audio.sample_rate,
            "bits_per_sample": audio.bits_per_sample,
            "channels": audio.channels,
            "frame_count": audio.frame_count,
            "duration_ms": audio.duration_ms,
            "duration_timecode": format_timecode(audio.duration_ms),
        },
        "detector": {
            **detector_params,
            "beat_count": len(beats),
            "beat_times_ms": [beat.time_ms for beat in beats],
        },
        "overhang": overhang,
        "summary": {
            "total_shots": len(entries),
            "completed_count": len(completed),
            "incomplete_count": len(incomplete),
            "preview_count": len(preview_paths) if preview_paths else 0,
        },
        "completed": completed,
        "incomplete": incomplete,
    }


def write_report(report: dict, report_path: str) -> str:
    """把核查结果写成 UTF-8、缩进 2 空格、保持键顺序的 JSON。"""
    parent = os.path.dirname(os.path.abspath(report_path))
    os.makedirs(parent, exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return os.path.abspath(report_path)
