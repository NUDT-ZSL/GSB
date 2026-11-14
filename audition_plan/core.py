"""试听方案的核心逻辑：校验输入、匹配片段、计算时间轴并输出结果。"""

from __future__ import annotations

import csv
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

CSV_FIELDS = [
    "event_id",
    "clip_id",
    "start_time_sec",
    "duration_sec",
    "end_time_sec",
    "overlap_warning",
    "missing_reason",
]

OVERLAP_WARNING = "重叠警告(OVERLAP)"
_MISSING_NO_MATCH = "无可用片段匹配所需标签"
_MISSING_EXHAUSTED = "匹配所需标签的可用片段均已被先前事件占用"


class InputError(ValueError):
    """输入文件格式错误、字段缺失或字段类型不合格。"""


@dataclass(frozen=True)
class Event:
    event_id: str
    time_sec: float
    required_tags: tuple[str, ...]
    order: int


@dataclass(frozen=True)
class Clip:
    clip_id: str
    path: str
    tags: tuple[str, ...]
    duration_sec: float
    order: int


@dataclass
class Row:
    event: Event
    clip: Clip | None
    start_sec: float
    overlap: bool = False
    missing_reason: str | None = None

    @property
    def end_sec(self) -> float | None:
        if self.clip is None:
            return None
        return self.start_sec + self.clip.duration_sec


@dataclass
class PlanResult:
    events_path: Path
    clips_path: Path
    rows: list[Row] = field(default_factory=list)
    unusable_clips: list[dict[str, Any]] = field(default_factory=list)

    @property
    def matched_count(self) -> int:
        return sum(1 for row in self.rows if row.clip is not None)

    @property
    def missing_count(self) -> int:
        return len(self.rows) - self.matched_count

    @property
    def overlap_count(self) -> int:
        return sum(1 for row in self.rows if row.overlap)

    def summary(self) -> dict[str, int]:
        return {
            "event_count": len(self.rows),
            "matched_count": self.matched_count,
            "missing_event_count": self.missing_count,
            "overlap_warning_count": self.overlap_count,
            "unusable_clip_count": len(self.unusable_clips),
        }


# ---------------------------------------------------------------- 读取与校验


def _load_json(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise InputError(f"无法读取输入文件 {path}: {exc}") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise InputError(f"输入文件 {path} 不是合法 JSON: {exc}") from exc


def _pick(obj: dict[str, Any], names: Iterable[str], canonical: str, ctx: str) -> Any:
    for name in names:
        if name in obj:
            return obj[name]
    raise InputError(f"{ctx} 缺少必需字段: {canonical}")


def _as_text(value: Any, ctx: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InputError(f"{ctx} 必须是非空字符串")
    return value.strip()


def _as_number(value: Any, ctx: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InputError(f"{ctx} 必须是数字")
    number = float(value)
    if not math.isfinite(number):
        raise InputError(f"{ctx} 必须是有限数字")
    return number


def _as_tags(value: Any, ctx: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise InputError(f"{ctx} 必须是字符串数组")
    tags: list[str] = []
    for index, item in enumerate(value):
        tag = _as_text(item, f"{ctx}[{index}]")
        if tag not in tags:
            tags.append(tag)
    return tuple(tags)


def _unwrap(payload: Any, key: str, ctx: str) -> list[Any]:
    if isinstance(payload, dict):
        if key not in payload:
            raise InputError(f"{ctx} 缺少必需字段: {key}")
        items = payload[key]
    elif isinstance(payload, list):
        items = payload
    else:
        raise InputError(f"{ctx} 根节点必须是对象或数组")
    if not isinstance(items, list):
        raise InputError(f"{ctx}.{key} 必须是数组")
    return items


def load_events(path: str | Path) -> list[Event]:
    source = Path(path).expanduser()
    events: list[Event] = []
    for index, item in enumerate(_unwrap(_load_json(source), "events", "事件表")):
        ctx = f"events[{index}]"
        if not isinstance(item, dict):
            raise InputError(f"{ctx} 必须是对象")
        event_id = _as_text(_pick(item, ("event_id", "id"), "event_id", ctx), f"{ctx}.event_id")
        time_sec = _as_number(_pick(item, ("time_sec", "time"), "time_sec", ctx), f"{ctx}.time_sec")
        if time_sec < 0:
            raise InputError(f"{ctx}.time_sec 不能为负数")
        tags = _as_tags(
            _pick(item, ("required_tags", "tags"), "required_tags", ctx),
            f"{ctx}.required_tags",
        )
        events.append(Event(event_id=event_id, time_sec=time_sec, required_tags=tags, order=index))
    return events


def _unusable_reason(base: Path, raw_path: str) -> tuple[str, str] | None:
    """返回 (解析后的路径, 不可用原因)；文件可读时返回 None。"""

    candidate = Path(raw_path).expanduser()
    resolved = candidate if candidate.is_absolute() else (base / candidate)
    resolved = resolved.resolve()
    if not resolved.exists():
        return str(resolved), "文件不存在"
    if not resolved.is_file():
        return str(resolved), "不是普通文件"
    try:
        with resolved.open("rb") as handle:
            handle.read(16)
    except OSError as exc:
        return str(resolved), f"文件无法读取: {exc.strerror or exc}"
    return None


def load_clips(path: str | Path) -> tuple[list[Clip], list[dict[str, Any]]]:
    source = Path(path).expanduser()
    base = source.resolve().parent
    clips: list[Clip] = []
    unusable: list[dict[str, Any]] = []
    for index, item in enumerate(_unwrap(_load_json(source), "clips", "片段清单")):
        ctx = f"clips[{index}]"
        if not isinstance(item, dict):
            raise InputError(f"{ctx} 必须是对象")
        clip_id = _as_text(_pick(item, ("clip_id", "id"), "clip_id", ctx), f"{ctx}.clip_id")
        raw_path = _as_text(_pick(item, ("path", "file"), "path", ctx), f"{ctx}.path")
        tags = _as_tags(_pick(item, ("tags",), "tags", ctx), f"{ctx}.tags")
        duration = _as_number(
            _pick(item, ("duration_sec", "duration"), "duration_sec", ctx),
            f"{ctx}.duration_sec",
        )
        if duration <= 0:
            raise InputError(f"{ctx}.duration_sec 必须大于 0")
        clip = Clip(
            clip_id=clip_id,
            path=raw_path,
            tags=tags,
            duration_sec=duration,
            order=index,
        )
        clips.append(clip)
        problem = _unusable_reason(base, raw_path)
        if problem is not None:
            resolved, reason = problem
            unusable.append(
                {
                    "index": index,
                    "clip_id": clip_id,
                    "path": raw_path,
                    "resolved_path": resolved,
                    "reason": reason,
                }
            )
    return clips, unusable


# ------------------------------------------------------------------ 匹配计算


def plan_audition(events_path: str | Path, clips_path: str | Path) -> PlanResult:
    events = load_events(events_path)
    clips, unusable = load_clips(clips_path)
    result = PlanResult(events_path=Path(events_path), clips_path=Path(clips_path),
                        unusable_clips=unusable)

    unusable_ids = {entry["index"] for entry in unusable}
    usable = [clip for clip in clips if clip.order not in unusable_ids]
    used: set[str] = set()

    # 时间升序；时间相同的按输入出现顺序（sorted 稳定，order 兜底）
    ordered = sorted(events, key=lambda event: (event.time_sec, event.order))

    for event in ordered:
        candidates = [
            clip
            for clip in usable
            if clip.clip_id not in used
            and all(tag in clip.tags for tag in event.required_tags)
        ]
        if not candidates:
            tagged = [
                clip
                for clip in usable
                if all(tag in clip.tags for tag in event.required_tags)
            ]
            result.rows.append(
                Row(
                    event=event,
                    clip=None,
                    start_sec=event.time_sec,
                    missing_reason=_MISSING_EXHAUSTED if tagged else _MISSING_NO_MATCH,
                )
            )
            continue
        clip = candidates[0]
        used.add(clip.clip_id)
        result.rows.append(Row(event=event, clip=clip, start_sec=event.time_sec))

    _mark_overlaps(result.rows)
    return result


def _mark_overlaps(rows: list[Row]) -> None:
    """按开始时间扫描，把存在时间交叠的行两两标记。

    只比较"可能仍在播放"的行：开始时间已按升序排列，因此可以丢弃
    结束时间早于当前行开始时间的候选。
    """

    active: list[Row] = []
    for row in rows:
        if row.clip is None:
            continue
        active = [
            previous
            for previous in active
            if previous.start_sec + previous.clip.duration_sec > row.start_sec + 1e-9
        ]
        if active:
            row.overlap = True
            for previous in active:
                previous.overlap = True
        active.append(row)


# -------------------------------------------------------------------- 输出


def _format_seconds(value: float | None) -> str:
    return "" if value is None else f"{value:.3f}"


def _row_payload(row: Row) -> dict[str, Any]:
    return {
        "event_id": row.event.event_id,
        "clip_id": row.clip.clip_id if row.clip else None,
        "start_time_sec": round(row.start_sec, 3),
        "duration_sec": round(row.clip.duration_sec, 3) if row.clip else None,
        "end_time_sec": round(row.end_sec, 3) if row.end_sec is not None else None,
        "required_tags": list(row.event.required_tags),
        "overlap_warning": row.overlap,
        "missing_reason": row.missing_reason,
    }


def write_csv(result: PlanResult, path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for row in result.rows:
            writer.writerow(
                {
                    "event_id": row.event.event_id,
                    "clip_id": row.clip.clip_id if row.clip else "",
                    "start_time_sec": _format_seconds(row.start_sec),
                    "duration_sec": _format_seconds(
                        row.clip.duration_sec if row.clip else None
                    ),
                    "end_time_sec": _format_seconds(row.end_sec),
                    "overlap_warning": OVERLAP_WARNING if row.overlap else "",
                    "missing_reason": row.missing_reason or "",
                }
            )
    return target


def build_report(result: PlanResult) -> dict[str, Any]:
    return {
        "inputs": {
            "events": str(result.events_path),
            "clips": str(result.clips_path),
        },
        "summary": result.summary(),
        "timeline": [_row_payload(row) for row in result.rows],
        "unusable_clips": result.unusable_clips,
    }


def write_report(result: PlanResult, path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(build_report(result), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return target


def write_outputs(result: PlanResult, out_dir: str | Path) -> tuple[Path, Path]:
    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    return (
        write_csv(result, directory / "audition_plan.csv"),
        write_report(result, directory / "audition_plan_report.json"),
    )
