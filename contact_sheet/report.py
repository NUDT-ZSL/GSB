"""定位报告 JSON 的组装与确定性写入。"""

from __future__ import annotations

import json
import os

from .config import SheetConfig
from .errors import ContactSheetError

SCHEMA_VERSION = "1.0"
EMPTY_NOTE = "清单为空，无镜头"


def _compute_end_ms(records: list[dict], duration_ms: int) -> dict[int, int]:
    """按时间顺序计算每条清单的片段结束毫秒（下一条的起点，末条为视频时长）。"""
    ordered = sorted(records, key=lambda r: (r["ms"], r["frame"], r["row"]))
    end_by_row: dict[int, int] = {}
    for idx, rec in enumerate(ordered):
        if idx + 1 < len(ordered):
            end = ordered[idx + 1]["ms"]
        else:
            end = duration_ms
        end_by_row[rec["row"]] = max(end, rec["ms"])
    return end_by_row


def build_report(records: list[dict], anomalies: list[dict],
                 config: SheetConfig, video_path: str, csv_path: str,
                 config_path: str | None, info: dict) -> dict:
    end_by_row = _compute_end_ms(records, info["duration_ms"])

    segments = []
    located_count = unlocatable_count = merged_count = 0
    for rec in sorted(records, key=lambda r: (r["ms"], r["frame"], r["row"])):
        if rec["status"] == "located":
            located_count += 1
            if rec["merged"]:
                merged_count += 1
        else:
            unlocatable_count += 1

        segment = {
            "row": rec["row"],
            "frame": rec["frame"],
            "start_ms": rec["ms"],
            "end_ms": end_by_row[rec["row"]],
            "representative_frame": (
                rec["merged_into_frame"] if rec["merged"] else
                (rec["frame"] if rec["status"] == "located" else None)
            ),
            "status": rec["status"],
            "anomaly_reasons": rec["anomaly_reasons"],
            "unlocatable_reason": rec["reason"],
            "merged": rec["merged"],
            "merged_into_frame": rec["merged_into_frame"],
            "merged_sources": rec["merged_sources"],
        }
        segments.append(segment)

    is_empty = len(records) == 0 and len(anomalies) == 0
    note = EMPTY_NOTE if is_empty else None
    return {
        "schema_version": SCHEMA_VERSION,
        "input": {
            "video": os.path.abspath(video_path),
            "csv": os.path.abspath(csv_path),
            "config_path": os.path.abspath(config_path) if config_path else None,
        },
        "video": {
            "width": info["width"],
            "height": info["height"],
            "fps": info["fps"],
            "duration_ms": info["duration_ms"],
            "nb_frames": info["nb_frames"],
        },
        "config": config.to_dict(),
        "summary": {
            "total_segments": len(segments),
            "located": located_count,
            "unlocatable": unlocatable_count,
            "merged_away": merged_count,
            "anomaly_count": len(anomalies),
            "note": note,
        },
        "anomalies": anomalies,
        "segments": segments,
    }


def save_report(report: dict, out_path: str) -> None:
    """固定缩进/换行/编码写入，保证重复运行字节一致。"""
    try:
        with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(report, ensure_ascii=False, indent=2))
            fh.write("\n")
    except OSError as exc:
        raise ContactSheetError(f"定位报告 JSON 写入失败：{out_path}（{exc}）") from exc
