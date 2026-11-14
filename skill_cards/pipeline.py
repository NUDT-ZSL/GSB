"""清单校验、技能分类与音效评估（需求 1、3、4、5、7、8、12）。"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .camera import camera_at, fit_level, nearest_keyframe_gap
from .model import Keyframe, Sound, is_number


@dataclass(frozen=True)
class CompletedSkill:
    """完成检视卡的技能。"""

    position: int                       # 在清单中的位置（1 起）
    page_name: str                      # 页名/汇总标识（空名时为 #序号）
    name: str | None                    # 原名；空名为 None
    duration_ms: float
    canvas_w: int
    canvas_h: int
    keyframes: list[Keyframe]
    sounds: list[Sound]
    filename: str

    @property
    def total_sounds(self) -> int:
        return len(self.sounds)

    @property
    def out_of_bounds_count(self) -> int:
        return sum(1 for s in self.sounds if s.out_of_bounds)


@dataclass(frozen=True)
class RejectedSkill:
    position: int
    page_name: str
    reason: str


@dataclass
class RunResult:
    completed: list[CompletedSkill] = field(default_factory=list)
    skipped: list[RejectedSkill] = field(default_factory=list)       # 结构/字段问题
    unfinished: list[RejectedSkill] = field(default_factory=list)    # 值不允许

    @property
    def total_out_of_bounds(self) -> int:
        return sum(c.out_of_bounds_count for c in self.completed)


# ---------------------------------------------------------------- 校验辅助

def _page_name_for(position: int, name: str | None) -> str:
    if name is not None and name.strip():
        return name.strip()
    return f"#{position:02d}"


def _check_keyframes(raw: object) -> tuple[list[Keyframe] | None, str | None]:
    if not isinstance(raw, list) or len(raw) == 0:
        return None, "camera_keyframes 必须是非空数组"
    frames: list[Keyframe] = []
    for idx, kf in enumerate(raw):
        if not isinstance(kf, dict):
            return None, f"camera_keyframes[{idx}] 不是对象"
        for key in ("time_ms", "zoom", "center"):
            if key not in kf:
                return None, f"camera_keyframes[{idx}] 缺少字段 {key}"
        if not is_number(kf["time_ms"]) or not is_number(kf["zoom"]):
            return None, f"camera_keyframes[{idx}] 的 time_ms/zoom 必须是数字"
        center = kf["center"]
        if not isinstance(center, (list, tuple)) or len(center) != 2:
            return None, f"camera_keyframes[{idx}] 的 center 必须是含两个数字的数组"
        if not all(is_number(v) for v in center):
            return None, f"camera_keyframes[{idx}] 的 center 必须是含两个数字的数组"
        frames.append(
            Keyframe(float(kf["time_ms"]), float(kf["zoom"]),
                     float(center[0]), float(center[1]))
        )
    return frames, None


def _check_sounds(raw: object) -> tuple[list[tuple[float, str]] | None, str | None]:
    if not isinstance(raw, list):
        return None, "sounds 必须是数组"
    sounds: list[tuple[float, str]] = []
    for idx, item in enumerate(raw):
        if is_number(item):
            sounds.append((float(item), f"音效{idx + 1}"))
        elif isinstance(item, dict):
            if "t" not in item:
                return None, f"sounds[{idx}] 缺少字段 t"
            if not is_number(item["t"]):
                return None, f"sounds[{idx}] 的 t 必须是数字"
            label = item.get("label", f"音效{idx + 1}")
            if not isinstance(label, str):
                label = f"音效{idx + 1}"
            sounds.append((float(item["t"]), label))
        else:
            return None, f"sounds[{idx}] 必须是数字或含 t 的对象"
    return sounds, None


def classify_entry(entry: object, position: int) -> tuple[str, object]:
    """把清单条目分为 completed / skipped / unfinished。

    返回 (类别, CompletedSkill 或 RejectedSkill)。
    """
    if not isinstance(entry, dict):
        return "skipped", RejectedSkill(
            position, _page_name_for(position, None), "清单条目不是 JSON 对象"
        )

    # ---- 结构层校验：缺失/类型不符 -> 跳过名单 ----
    if "name" not in entry:
        return "skipped", RejectedSkill(
            position, _page_name_for(position, None), "缺少字段 name"
        )
    name_raw = entry["name"]
    if not isinstance(name_raw, str):
        return "skipped", RejectedSkill(
            position, _page_name_for(position, None), "name 必须是字符串"
        )

    if "duration_ms" not in entry:
        return "skipped", RejectedSkill(
            position, _page_name_for(position, name_raw), "缺少字段 duration_ms"
        )
    if not is_number(entry["duration_ms"]):
        return "skipped", RejectedSkill(
            position, _page_name_for(position, name_raw), "duration_ms 必须是数字"
        )

    if "canvas" not in entry:
        return "skipped", RejectedSkill(
            position, _page_name_for(position, name_raw), "缺少字段 canvas"
        )
    canvas = entry["canvas"]
    if not isinstance(canvas, dict):
        return "skipped", RejectedSkill(
            position, _page_name_for(position, name_raw), "canvas 必须是对象"
        )
    for key in ("width", "height"):
        if key not in canvas:
            return "skipped", RejectedSkill(
                position, _page_name_for(position, name_raw),
                f"canvas 缺少字段 {key}"
            )
        if not is_number(canvas[key]):
            return "skipped", RejectedSkill(
                position, _page_name_for(position, name_raw),
                f"canvas.{key} 必须是数字"
            )

    if "camera_keyframes" not in entry:
        return "skipped", RejectedSkill(
            position, _page_name_for(position, name_raw),
            "缺少字段 camera_keyframes"
        )
    frames, err = _check_keyframes(entry["camera_keyframes"])
    if err is not None:
        return "skipped", RejectedSkill(
            position, _page_name_for(position, name_raw), err
        )

    if "sounds" not in entry:
        return "skipped", RejectedSkill(
            position, _page_name_for(position, name_raw), "缺少字段 sounds"
        )
    raw_sounds, err = _check_sounds(entry["sounds"])
    if err is not None:
        return "skipped", RejectedSkill(
            position, _page_name_for(position, name_raw), err
        )

    page_name = _page_name_for(position, name_raw)
    duration = float(entry["duration_ms"])
    width = float(canvas["width"])
    height = float(canvas["height"])

    # ---- 值层校验：非正值 -> 未完成名单（需求 7）----
    if not duration > 0:
        return "unfinished", RejectedSkill(
            position, page_name, f"总时长 duration_ms={format_num(duration)} 不大于 0"
        )
    if not width > 0 or not height > 0:
        return "unfinished", RejectedSkill(
            position, page_name,
            f"画布尺寸 width={format_num(width)}, height={format_num(height)} 存在不大于 0 的值"
        )

    # ---- 关键帧规范化：同时刻去重（后者覆盖）并升序 ----
    by_time: dict[float, Keyframe] = {}
    for kf in frames:  # type: ignore[union-attr]
        by_time[kf.time_ms] = kf
    keyframes = sorted(by_time.values(), key=lambda kf: kf.time_ms)

    # ---- 音效评估（需求 3、4）----
    evaluated: list[Sound] = []
    for t, label in raw_sounds:  # type: ignore[union-attr]
        if t < 0 or t > duration:
            # 越界：标为越界并排除出贴合度计算，间隔留空
            evaluated.append(Sound(t=t, label=label, out_of_bounds=True))
            continue
        zoom, cx, cy = camera_at(keyframes, t)
        gap, nearest_t = nearest_keyframe_gap(keyframes, t)
        evaluated.append(Sound(
            t=t, label=label, out_of_bounds=False,
            zoom=zoom, cx=cx, cy=cy,
            gap_ms=gap, nearest_kf_ms=nearest_t, fit=fit_level(gap),
        ))

    # 页内音效按时刻升序；同时刻保持清单顺序（稳定排序）
    evaluated.sort(key=lambda s: s.t)

    return "completed", CompletedSkill(
        position=position,
        page_name=page_name,
        name=(name_raw.strip() or None),
        duration_ms=duration,
        canvas_w=int(round(width)),
        canvas_h=int(round(height)),
        keyframes=keyframes,
        sounds=evaluated,
        filename=build_filename(page_name, position),
    )


def build_filename(page_name: str, position: int) -> str:
    """生成安全文件名：序号前缀 + 页名，非法字符替换。"""
    cleaned = page_name
    for ch in '<>:"/\\|?*':
        cleaned = cleaned.replace(ch, "_")
    cleaned = cleaned.strip().strip(".")
    if not cleaned:
        cleaned = f"skill-{position:02d}"
    return f"{position:02d}_{cleaned}.png"


def run_pipeline(entries: list[object]) -> RunResult:
    result = RunResult()
    for idx, entry in enumerate(entries, start=1):
        category, payload = classify_entry(entry, idx)
        if category == "completed":
            result.completed.append(payload)  # type: ignore[arg-type]
        elif category == "skipped":
            result.skipped.append(payload)  # type: ignore[arg-type]
        else:
            result.unfinished.append(payload)  # type: ignore[arg-type]

    # 检视卡按页名（技能名）升序（需求 5）；名单保持清单位置顺序，原因随条目
    result.completed.sort(key=lambda c: c.page_name)
    return result


def format_num(value: float | None, digits: int = 3) -> str:
    """统一数字格式：去掉浮点尾差，None 显示留空标记。"""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "—"
    text = f"{value:.{digits}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text
