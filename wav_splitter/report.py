"""JSON 剪辑报告生成：排序规则、条目结构与文件写出。"""

from __future__ import annotations

import json
import os

from .models import (
    SegmentResult,
    STATUS_INVALID_INTERVAL,
    STATUS_OUT_OF_RANGE,
    STATUS_OVERLAP,
    STATUS_SUCCESS,
)

REPORT_FILENAME = "report.json"


def sort_results(results: list[SegmentResult]) -> list[SegmentResult]:
    """报告排序：编号大小写不敏感升序；编号相同时按 CSV 原始行号升序。"""
    return sorted(
        results,
        key=lambda r: (r.segment_id.casefold(), r.row_number),
    )


def _entry(result: SegmentResult) -> dict:
    """单条报告条目；跳过的片段不携带任何导出文件/采样点字段。"""
    entry = {
        "编号": result.segment_id,
        "csv行号": result.row_number,
        "原始起始时间": result.raw_start,
        "原始结束时间": result.raw_end,
        "状态": result.status,
        "错误描述": result.error,  # 成功时严格为 null
    }
    if result.status == STATUS_SUCCESS:
        entry["导出文件"] = result.output_file
        entry["起始采样点"] = result.start_sample
        entry["结束采样点"] = result.end_sample
    return entry


def build_report(results: list[SegmentResult], info, wav_path: str,
                 csv_path: str) -> dict:
    """组装完整报告对象（结构说明见 REPORT_SCHEMA.md）。"""
    ordered = sort_results(results)
    counts = {
        STATUS_SUCCESS: 0,
        STATUS_OUT_OF_RANGE: 0,
        STATUS_INVALID_INTERVAL: 0,
        STATUS_OVERLAP: 0,
    }
    for result in results:
        counts[result.status] += 1

    return {
        "报告版本": "1.0",
        "源文件": {
            "wav路径": wav_path,
            "csv路径": csv_path,
        },
        "音频参数": {
            "声道数": info.channels,
            "采样率Hz": info.frame_rate,
            "位深bit": info.sample_width * 8,
            "总采样帧数": info.frame_count,
            "总时长秒": info.duration_seconds,
        },
        "统计": {
            "标记总数": len(results),
            "成功数": counts[STATUS_SUCCESS],
            "越界数": counts[STATUS_OUT_OF_RANGE],
            "无效区间数": counts[STATUS_INVALID_INTERVAL],
            "重叠数": counts[STATUS_OVERLAP],
        },
        "片段结果": [_entry(r) for r in ordered],
    }


def write_report(report: dict, output_dir: str) -> str:
    """把报告写入输出目录，返回报告路径。"""
    path = os.path.join(output_dir, REPORT_FILENAME)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    return path
