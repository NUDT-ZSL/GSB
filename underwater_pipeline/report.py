"""Deterministic JSON/CSV/Markdown result writers."""

from __future__ import annotations

import csv
import json
import os
from pathlib import Path
from typing import Any

import numpy as np

from .storage import save_npz_deterministic


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def write_json(path: str | Path, value: Any) -> Path:
    destination = Path(path)
    _atomic_text(
        destination,
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n",
    )
    return destination


def _write_csv(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    os.replace(temporary, path)


def build_report(result: dict) -> dict:
    metadata = result["metadata"]
    shots = result["visibility"]["shots"]
    segments = result["teaching_segments"]
    events = result["events"]
    shot_by_id = {item["shot_id"]: item for item in shots}

    segment_records: list[dict] = []
    for segment in segments:
        segment_shots = [shot_by_id[value] for value in segment["shot_ids"]]
        segment_events = [
            event
            for event in events
            if event["event_id"] in set(segment["event_ids"])
        ]
        changes = [
            point
            for point in result["turbidity_change_points"]
            if point["change_id"] in set(segment["turbidity_change_ids"])
        ]
        segment_records.append(
            {
                "segment_id": segment["segment_id"],
                "start_frame": segment["start_frame"],
                "start_frame_number": segment["start_frame"] + 1,
                "end_frame_exclusive": segment["end_frame_exclusive"],
                "end_frame_number": segment["end_frame_exclusive"],
                "start_time_s": segment["start_time_s"],
                "end_time_s": segment["end_time_s"],
                "shot_ids": segment["shot_ids"],
                "shot_count": segment["shot_count"],
                "fitted_visibility_score": segment[
                    "fitted_visibility_score"
                ],
                "visibility_grade_min": segment["visibility_grade_min"],
                "visibility_grade_max": segment["visibility_grade_max"],
                "visibility_level_span_names": segment[
                    "visibility_level_span_names"
                ],
                "event_ids": segment["event_ids"],
                "event_types": segment["event_types"],
                "turbidity_change_ids": segment[
                    "turbidity_change_ids"
                ],
                "shots": [
                    {
                        "shot_id": shot["shot_id"],
                        "start_frame": shot["start_frame"],
                        "end_frame_exclusive": shot[
                            "end_frame_exclusive"
                        ],
                        "visibility_score": shot["score"],
                        "turbidity_level": shot["turbidity_level"],
                        "turbidity_level_name": shot[
                            "turbidity_level_name"
                        ],
                    }
                    for shot in segment_shots
                ],
                "turbidity_changes": changes,
                "events": segment_events,
            }
        )

    return {
        "schema_version": "1.0",
        "title": "潜水训练视频教学分段复核报告",
        "metadata": metadata,
        "result_content_hash": result["content_hash"],
        "summary": {
            "frame_count": metadata["frame_count"],
            "raw_boundary_count": result["boundaries"]["summary"]["raw_count"],
            "stable_shot_count": len(shots),
            "stable_boundary_count": result["boundaries"]["summary"][
                "stable_count"
            ],
            "teaching_segment_count": len(segments),
            "event_count": len(events),
            "turbidity_interval_count": len(
                result["turbidity_intervals"]
            ),
            "turbidity_change_count": len(
                result["turbidity_change_points"]
            ),
        },
        "consistency": result["consistency"],
        "shot_ownership": {
            "shot_ids": [shot["shot_id"] for shot in shots],
            "segment_shot_ids": [
                [shot_id for shot_id in segment["shot_ids"]]
                for segment in segments
            ],
            "flat_segment_shot_ids": [
                shot_id
                for segment in segments
                for shot_id in segment["shot_ids"]
            ],
            "all_match": result["consistency"]["all_passed"],
        },
        "segments": segment_records,
        "event_table": events,
        "turbidity_intervals": result["turbidity_intervals"],
        "truth_comparison": result["truth_comparison"],
    }


def write_result_bundle(
    output_directory: str | Path,
    result: dict,
) -> dict[str, str]:
    """Write all reviewable artifacts and return their repository paths."""

    directory = Path(output_directory)
    directory.mkdir(parents=True, exist_ok=True)
    report = build_report(result)

    artifacts: dict[str, str] = {}
    artifacts["analysis"] = str(
        write_json(directory / "analysis.json", result)
    )
    artifacts["boundaries"] = str(
        write_json(
            directory / "shot_boundaries.json",
            {
                "metadata": result["metadata"],
                "boundaries": result["boundaries"],
                "truth_comparison": result["truth_comparison"],
                "content_hash": result["content_hash"],
            },
        )
    )
    artifacts["visibility"] = str(
        write_json(
            directory / "visibility_scores.json",
            {
                "metadata": result["metadata"],
                "visibility": result["visibility"],
                "piecewise_fit": result["piecewise_fit"],
                "content_hash": result["content_hash"],
            },
        )
    )
    artifacts["segments"] = str(
        write_json(
            directory / "teaching_segments.json",
            {
                "metadata": result["metadata"],
                "teaching_segments": result[
                    "teaching_segments"
                ],
                "turbidity_change_points": result[
                    "turbidity_change_points"
                ],
                "content_hash": result["content_hash"],
            },
        )
    )
    artifacts["events"] = str(
        write_json(
            directory / "events.json",
            {
                "metadata": result["metadata"],
                "events": result["events"],
                "detector_collisions": result["detector_collisions"],
                "turbidity_intervals": result["turbidity_intervals"],
                "content_hash": result["content_hash"],
            },
        )
    )
    artifacts["report_json"] = str(
        write_json(directory / "report.json", report)
    )

    _write_csv(
        directory / "frame_metrics.csv",
        result["frame_metrics"],
        [
            "frame_index",
            "frame_number",
            "time_s",
            "hist_distance",
            "edge_density",
            "edge_density_change",
            "boundary_metric",
            "adaptive_threshold",
            "visibility_score",
            "smoothed_visibility_score",
            "shot_id",
        ],
    )
    artifacts["frame_metrics_csv"] = str(directory / "frame_metrics.csv")

    _write_csv(
        directory / "shot_scores.csv",
        [
            {
                **shot,
                "turbidity_level_name": shot["turbidity_level_name"],
            }
            for shot in result["visibility"]["shots"]
        ],
        [
            "shot_id",
            "start_frame",
            "end_frame_exclusive",
            "frame_count",
            "start_time_s",
            "end_time_s",
            "duration_s",
            "score",
            "score_std",
            "score_median",
            "score_min",
            "score_max",
            "score_p10",
            "score_p90",
            "turbidity_level",
            "turbidity_level_name",
            "frames_used",
            "local_contrast_mean",
            "local_contrast_norm_mean",
            "dark_channel_mean",
            "dark_channel_p90_mean",
            "dark_channel_score_mean",
            "saturation_mean",
        ],
    )
    artifacts["shot_scores_csv"] = str(directory / "shot_scores.csv")

    _write_csv(
        directory / "events.csv",
        result["events"],
        [
            "event_id",
            "frame_index",
            "frame_number",
            "time_s",
            "event_type",
            "decision",
            "cut_evidence",
            "visibility_jump",
            "visibility_evidence",
            "shot_id_after",
            "turbidity_interval_index",
        ],
    )
    artifacts["events_csv"] = str(directory / "events.csv")

    markdown = _render_markdown(report, result)
    _atomic_text(directory / "report.md", markdown)
    artifacts["report_md"] = str(directory / "report.md")

    return artifacts


def _render_markdown(report: dict, result: dict) -> str:
    metadata = report["metadata"]
    summary = report["summary"]
    lines = [
        "# 潜水训练视频教学分段复核报告",
        "",
        "## 运行摘要",
        "",
        f"- 随机种子：`{metadata['seed']}`",
        f"- 帧数：`{summary['frame_count']}`",
        f"- 原始/稳定边界：`{summary['raw_boundary_count']}` / "
        f"`{summary['stable_boundary_count']}`",
        f"- 稳定镜头：`{summary['stable_shot_count']}`",
        f"- 教学段落：`{summary['teaching_segment_count']}`",
        f"- 事件：`{summary['event_count']}`",
        f"- 浑浊区间：`{summary['turbidity_interval_count']}`",
        f"- 内容哈希：`{report['result_content_hash']}`",
        "",
        "## 镜头切分与修正",
        "",
        "| 边界帧号 | 时间(s) | 处理 | 原因 |",
        "| ---: | ---: | --- | --- |",
    ]
    for change in result["boundaries"]["correction_changes"]:
        lines.append(
            f"| {change['frame_number']} | {change['time_s']:.3f} | "
            f"{change['change']} | {change['reason']} |"
        )

    lines.extend(
        [
            "",
            "## 分段与能见度",
            "",
            "| 段落 | 帧范围 | 镜头 | 拟合分数 | 等级跨度 | 事件类型 |",
            "| --- | --- | --- | ---: | --- | --- |",
        ]
    )
    for segment in report["segments"]:
        frame_range = (
            f"{segment['start_frame_number']}-"
            f"{segment['end_frame_number']}"
        )
        lines.append(
            f"| {segment['segment_id']} | {frame_range} | "
            f"{', '.join(segment['shot_ids'])} | "
            f"{segment['fitted_visibility_score']:.2f} | "
            f"{segment['visibility_grade_min']}-"
            f"{segment['visibility_grade_max']} | "
            f"{', '.join(segment['event_types']) or '-'} |"
        )

    lines.extend(
        [
            "",
            "## 事件表",
            "",
            "| 事件 | 帧号 | 时间(s) | 类型 | 裁定依据 | 镜头 |",
            "| --- | ---: | ---: | --- | --- | --- |",
        ]
    )
    for event in report["event_table"]:
        lines.append(
            f"| {event['event_id']} | {event['frame_number']} | "
            f"{event['time_s']:.3f} | {event['event_type']} | "
            f"{event['decision']} | {event['shot_id_after'] or '-'} |"
        )

    lines.extend(
        [
            "",
            "## 一致性",
            "",
        ]
    )
    for name, passed in report["consistency"]["checks"].items():
        lines.append(f"- `{name}`: {'PASS' if passed else 'FAIL'}")
    lines.append("")
    return "\n".join(lines)


def write_sample_file(
    directory: str | Path,
    frames: np.ndarray,
    timestamps: np.ndarray,
    truth: dict,
) -> Path:
    path = Path(directory) / "sample.npz"
    truth_json = np.asarray(
        json.dumps(truth, ensure_ascii=False, sort_keys=True)
    )
    return save_npz_deterministic(
        path,
        {
            "frames": frames,
            "timestamps": timestamps,
            "truth_json": truth_json,
        },
    )
