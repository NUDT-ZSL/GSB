"""核心剪辑管线：越界/无效/重叠隔离，合法片段切片导出。"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass

from .models import (
    Marker,
    SegmentResult,
    STATUS_INVALID_INTERVAL,
    STATUS_OUT_OF_RANGE,
    STATUS_OVERLAP,
    STATUS_SUCCESS,
)
from .wav_io import (
    is_within_duration,
    time_to_sample_index,
    write_segment,
)

# Windows 与常见文件系统不允许的文件名字符；同时去掉控制字符
_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


@dataclass(frozen=True)
class _AcceptedSegment:
    """已成功导出的片段，供后续标记做重叠判定。"""

    start_sample: int
    end_sample: int


def build_output_filename(segment_id: str, row_number: int, used: set[str]) -> str:
    """生成安全且唯一的导出文件名（不含目录）。

    - 过滤文件系统非法字符（防止路径穿越等问题）
    - 编号相同导致重名时，后出现者追加 CSV 原始行号区分
    """
    safe = _INVALID_FILENAME_CHARS.sub("_", segment_id).strip().rstrip(". ")
    if safe in ("", ".."):
        safe = f"segment_{row_number}"
    candidate = f"{safe}.wav"
    if candidate not in used:
        used.add(candidate)
        return candidate
    candidate = f"{safe}__row{row_number}.wav"
    used.add(candidate)
    return candidate


def _is_overlapping(start_sample: int, end_sample: int,
                    accepted: list[_AcceptedSegment]) -> bool:
    """端点重叠也算重叠：区间 [a,b] 与 [c,d] 满足 a <= d 且 c <= b。"""
    return any(
        start_sample <= seg.end_sample and seg.start_sample <= end_sample
        for seg in accepted
    )


def process_markers(
    markers: list[Marker],
    info,
    pcm: bytes,
    output_dir: str,
    warn=lambda message: print(message, file=sys.stderr),
) -> list[SegmentResult]:
    """按 CSV 原始顺序处理全部标记，返回每个标记的处理结果。

    判定顺序（对同一行短路）：越界 -> 无效区间 -> 重叠 -> 成功导出。
    跳过的片段通过 ``warn`` 输出含编号与原因的警告（默认写 stderr）。
    """
    results: list[SegmentResult] = []
    accepted: list[_AcceptedSegment] = []
    used_names: set[str] = set()

    for marker in markers:
        # 1) 越界：任一端点超出 [0, 总时长]
        if not is_within_duration(marker.start, marker.end, info):
            reason = (
                f"警告：片段 {marker.segment_id}（CSV 第 {marker.row_number} 行）"
                f"跳过：越界，原始区间 [{marker.raw_start}, {marker.raw_end}]，"
                f"音频总时长 {info.duration_seconds:.6f} 秒"
            )
            warn(reason)
            results.append(
                SegmentResult(
                    segment_id=marker.segment_id,
                    row_number=marker.row_number,
                    raw_start=marker.raw_start,
                    raw_end=marker.raw_end,
                    status=STATUS_OUT_OF_RANGE,
                    error=(
                        f"起止时间超出音频范围（音频总时长 "
                        f"{info.duration_seconds:.6f} 秒）"
                    ),
                )
            )
            continue

        # 2) 无效区间：结束 <= 起始
        if marker.end <= marker.start:
            reason = (
                f"警告：片段 {marker.segment_id}（CSV 第 {marker.row_number} 行）"
                f"跳过：无效区间，结束时间 {marker.raw_end} "
                f"小于或等于起始时间 {marker.raw_start}"
            )
            warn(reason)
            results.append(
                SegmentResult(
                    segment_id=marker.segment_id,
                    row_number=marker.row_number,
                    raw_start=marker.raw_start,
                    raw_end=marker.raw_end,
                    status=STATUS_INVALID_INTERVAL,
                    error="结束时间小于或等于起始时间",
                )
            )
            continue

        # 毫秒级对齐到采样点
        start_sample = time_to_sample_index(marker.start, info.frame_rate)
        end_sample = time_to_sample_index(marker.end, info.frame_rate)

        # 对齐后若退化为空区间，同样按无效区间处理
        if end_sample <= start_sample:
            reason = (
                f"警告：片段 {marker.segment_id}（CSV 第 {marker.row_number} 行）"
                f"跳过：无效区间，毫秒级对齐后长度为 0"
            )
            warn(reason)
            results.append(
                SegmentResult(
                    segment_id=marker.segment_id,
                    row_number=marker.row_number,
                    raw_start=marker.raw_start,
                    raw_end=marker.raw_end,
                    status=STATUS_INVALID_INTERVAL,
                    error="毫秒级对齐后区间长度为 0",
                )
            )
            continue

        # 3) 重叠：与此前已成功导出的片段（含端点）相交，后出现者跳过
        if _is_overlapping(start_sample, end_sample, accepted):
            reason = (
                f"警告：片段 {marker.segment_id}（CSV 第 {marker.row_number} 行）"
                f"跳过：重叠，与此前已导出的片段区间相交"
            )
            warn(reason)
            results.append(
                SegmentResult(
                    segment_id=marker.segment_id,
                    row_number=marker.row_number,
                    raw_start=marker.raw_start,
                    raw_end=marker.raw_end,
                    status=STATUS_OVERLAP,
                    error="与此前已导出的片段区间重叠（含端点）",
                )
            )
            continue

        # 4) 合法片段：切片写出，头部参数与原文件完全一致
        filename = build_output_filename(marker.segment_id, marker.row_number,
                                         used_names)
        output_path = os.path.join(output_dir, filename)
        write_segment(output_path, info, pcm, start_sample, end_sample)
        accepted.append(_AcceptedSegment(start_sample, end_sample))
        results.append(
            SegmentResult(
                segment_id=marker.segment_id,
                row_number=marker.row_number,
                raw_start=marker.raw_start,
                raw_end=marker.raw_end,
                status=STATUS_SUCCESS,
                error=None,
                output_file=filename,
                start_sample=start_sample,
                end_sample=end_sample,
            )
        )

    return results
