"""片段清单（JSON）的解析与逐项校验。

清单顶层必须是一个 JSON 数组，元素为对象：
    {"start": 1.25, "end": 2.0, "gain": 0.75}
- start/end 为秒，可选 gain 默认 1.0；
- 清单项问题不会中止整个任务，而是逐项记录原因并跳过。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .wavio import AudioError

GAIN_MIN = 0.0
GAIN_MAX = 4.0


@dataclass(frozen=True)
class ClipError:
    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


@dataclass
class ClipEntry:
    index: int
    start: Any = None
    end: Any = None
    gain_raw: Any = None
    gain: float | None = None
    errors: list[ClipError] = field(default_factory=list)
    start_frame: int | None = None
    end_frame: int | None = None

    @property
    def label(self) -> str:
        return f"#{self.index + 1}"

    @property
    def frame_count(self) -> int | None:
        if self.start_frame is None or self.end_frame is None:
            return None
        return self.end_frame - self.start_frame

    @property
    def valid(self) -> bool:
        return not self.errors

    @property
    def primary_reason(self) -> str | None:
        return self.errors[0].code if self.errors else None


def load_manifest(path: str) -> list[Any]:
    """读取清单 JSON，返回原始条目数组；结构性错误抛 AudioError。"""
    manifest = Path(path)
    if not manifest.exists():
        raise AudioError("manifest_missing", f"片段清单不存在：{path}")
    try:
        text = manifest.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise AudioError("manifest_unreadable", f"无法读取片段清单：{exc}") from exc
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AudioError(
            "manifest_invalid_json",
            f"片段清单不是合法 JSON（第 {exc.lineno} 行第 {exc.colno} 列）：{exc.msg}",
        ) from exc
    if not isinstance(payload, list):
        raise AudioError(
            "manifest_invalid_structure",
            f"片段清单顶层必须是数组，实际为 {type(payload).__name__}",
        )
    return payload


def _is_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def seconds_to_frame(seconds: float, sample_rate: int) -> int:
    """秒 -> 采样帧边界，0.5 远离 0 取整，避免银行家舍入造成的不确定。"""
    scaled = float(seconds) * sample_rate
    if scaled >= 0:
        return int(math.floor(scaled + 0.5))
    return -int(math.floor(-scaled + 0.5))


def validate_entry(index: int, raw: Any, sample_rate: int, total_frames: int) -> ClipEntry:
    entry = ClipEntry(index=index)

    if not isinstance(raw, dict):
        entry.errors.append(ClipError(
            "invalid_entry",
            f"清单项必须是 JSON 对象，实际为 {type(raw).__name__}",
        ))
        return entry

    has_start = "start" in raw
    has_end = "end" in raw
    if not has_start or not has_end:
        missing = [k for k, present in (("start", has_start), ("end", has_end))
                   if not present]
        entry.errors.append(ClipError(
            "invalid_entry",
            f"清单项缺少必填字段：{', '.join(missing)}",
        ))
        return entry

    entry.start = raw.get("start")
    entry.end = raw.get("end")
    if not _is_number(entry.start):
        entry.errors.append(ClipError(
            "invalid_entry", f"start 必须是有限数值，实际为 {entry.start!r}",
        ))
    if not _is_number(entry.end):
        entry.errors.append(ClipError(
            "invalid_entry", f"end 必须是有限数值，实际为 {entry.end!r}",
        ))

    if "gain" in raw:
        entry.gain_raw = raw.get("gain")
        if _is_number(entry.gain_raw) and GAIN_MIN <= float(entry.gain_raw) <= GAIN_MAX:
            entry.gain = float(entry.gain_raw)
        else:
            entry.errors.append(ClipError(
                "invalid_gain",
                f"gain 必须是 [{GAIN_MIN}, {GAIN_MAX}] 内的数值，实际为 {entry.gain_raw!r}",
            ))
    else:
        entry.gain = 1.0

    if entry.errors:
        return entry

    start = float(entry.start)
    end = float(entry.end)
    if start >= end:
        entry.errors.append(ClipError(
            "start_after_end",
            f"start 必须小于 end，实际 start={start}，end={end}",
        ))
        return entry
    if start < 0:
        entry.errors.append(ClipError(
            "negative_start", f"start 不得为负数，实际为 {start}",
        ))
        return entry

    entry.start_frame = seconds_to_frame(start, sample_rate)
    entry.end_frame = seconds_to_frame(end, sample_rate)
    duration = total_frames / sample_rate

    if entry.start_frame > total_frames or entry.end_frame > total_frames:
        entry.errors.append(ClipError(
            "out_of_bounds",
            f"片段 [{start}, {end}] 秒超出源时长 {duration:.6f} 秒"
            f"（{total_frames} 帧）",
        ))
        return entry

    if entry.end_frame - entry.start_frame <= 0:
        entry.errors.append(ClipError(
            "zero_length",
            f"片段 [{start}, {end}] 秒换算后不足 1 个采样帧",
        ))
        return entry

    return entry


def validate_entries(raw_entries: list[Any], sample_rate: int, total_frames: int) -> list[ClipEntry]:
    return [validate_entry(i, raw, sample_rate, total_frames)
            for i, raw in enumerate(raw_entries)]


def validate_entries_without_source(raw_entries: list[Any]) -> list[ClipEntry]:
    """源不可用时的降级校验：只检查条目结构与 gain，不做时间边界判断。"""
    entries: list[ClipEntry] = []
    for index, raw in enumerate(raw_entries):
        entry = ClipEntry(index=index)
        if not isinstance(raw, dict):
            entry.errors.append(ClipError(
                "invalid_entry",
                f"清单项必须是 JSON 对象，实际为 {type(raw).__name__}",
            ))
            entries.append(entry)
            continue
        entry.start = raw.get("start")
        entry.end = raw.get("end")
        if "gain" in raw:
            entry.gain_raw = raw.get("gain")
            if _is_number(entry.gain_raw) and GAIN_MIN <= float(entry.gain_raw) <= GAIN_MAX:
                entry.gain = float(entry.gain_raw)
            else:
                entry.errors.append(ClipError(
                    "invalid_gain",
                    f"gain 必须是 [{GAIN_MIN}, {GAIN_MAX}] 内的数值，"
                    f"实际为 {entry.gain_raw!r}",
                ))
        else:
            entry.gain = 1.0
        entries.append(entry)
    return entries
