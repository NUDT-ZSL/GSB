# -*- coding: utf-8 -*-
"""2D 骨骼动画数据模型：加载/校验、插值、层级姿态合成、姿态报告导出。

JSON 结构（顶层）::

    {
      "name": "动画名(可选)",
      "duration": 总时长(可选, 缺省取最大关键帧时间),
      "bones": [
        {
          "name": "骨骼名",
          "parent": "父骨骼名(可选)",
          "length": 绘制长度(可选, 默认 80),
          "keyframes": [
            {"time": 0.0, "translation": [x, y],
             "rotation": 角度, "scale": [sx, sy]},
            ...
          ]
        }
      ]
    }

关键帧缺失字段按 平移(0,0)、旋转 0、缩放 (1,1) 处理。
"""
from __future__ import annotations

import json
import math
import os
from bisect import bisect_right
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------- 常量 / 数据结构

DEFAULT_TRANSLATION = (0.0, 0.0)
DEFAULT_ROTATION = 0.0
DEFAULT_SCALE = (1.0, 1.0)
DEFAULT_BONE_LENGTH = 80.0


@dataclass
class Keyframe:
    """单个关键帧（已通过格式校验、字段已补齐默认值）。"""
    time: float
    translation: Tuple[float, float]
    rotation: float
    scale: Tuple[float, float]


@dataclass
class SkippedFrame:
    """被跳过的异常关键帧记录（用于状态区展示，需求 6）。"""
    raw_time: float          # 原始时间数值（曲线视图警示带定位用）
    time_repr: str           # 原始时间值的展示文本，负数/倒序时保留原始输入
    reason: str


@dataclass
class BoneIssue:
    """某根骨骼上的问题记录（重复时间/异常关键帧/格式错误，需求 4/6）。"""
    messages: List[str] = field(default_factory=list)
    skipped: List[SkippedFrame] = field(default_factory=list)


@dataclass
class Bone:
    name: str
    parent: Optional[str]
    length: float
    keyframes: List[Keyframe]                 # 仅保留正常关键帧，按时间升序
    times: List[float] = field(default_factory=list)
    issue: BoneIssue = field(default_factory=BoneIssue)
    # 运行期由 compose_pose 填充
    origin: Tuple[float, float] = (0.0, 0.0)
    world_rotation: float = 0.0
    world_scale: Tuple[float, float] = (1.0, 1.0)
    tip: Tuple[float, float] = (0.0, 0.0)


@dataclass
class Animation:
    name: str
    duration: float
    bones: List[Bone]
    by_name: Dict[str, Bone]
    total_keyframes: int          # 原始关键帧总数（含被跳过的，需求 10）
    load_error: Optional[str]     # 文件级格式错误；非 None 时 bones 为空
    file_path: Optional[str] = None
    dirty: bool = False           # 姿态是否已变化且尚未导出（需求 11）

    # ---------------------------------------------------------- 查询辅助
    def abnormal_bones(self) -> List[Bone]:
        """存在问题（重复时间/跳过帧/格式错误）的骨骼，需求 10 异常骨骼计数。"""
        return [b for b in self.bones if b.issue.messages]

    def selected_or_first(self, selected: Optional[str]) -> Optional[Bone]:
        if selected and selected in self.by_name:
            return self.by_name[selected]
        return self.bones[0] if self.bones else None


# ---------------------------------------------------------------- 工具函数

def _is_number(v: Any) -> bool:
    # bool 是 int 的子类，显式排除，避免 True/False 被当作数值
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _as_vec2(value: Any, default: Tuple[float, float],
             what: str, issues: List[str]) -> Tuple[float, float]:
    """解析二维向量字段，元素非法时该分量回落默认值并记录原因。"""
    if value is None:
        return default
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        issues.append(f"{what} 格式错误，应为含两个数字的数组，已按默认值填充")
        return default
    out = list(default)
    for i, part in enumerate(value):
        if _is_number(part):
            out[i] = float(part)
        else:
            issues.append(f"{what}[{i}] 不是数字，该分量按默认值填充")
    return out[0], out[1]


def _parse_keyframe(raw: Any, issues: List[str]) -> Optional[Keyframe]:
    """把一条原始记录解析为 Keyframe；格式错误返回 None 并写入 issues。"""
    if not isinstance(raw, dict):
        issues.append(f"关键帧不是对象: {raw!r}，已跳过该条")
        return None
    t = raw.get("time")
    if not _is_number(t):
        issues.append(f"关键帧缺少合法 time 字段: {raw!r}，已跳过该条")
        return None
    translation = _as_vec2(raw.get("translation"), DEFAULT_TRANSLATION,
                           "translation", issues)
    rotation = raw.get("rotation")
    if rotation is None:
        rot = DEFAULT_ROTATION
    elif _is_number(rotation):
        rot = float(rotation)
    else:
        issues.append("rotation 不是数字，按 0 处理")
        rot = DEFAULT_ROTATION
    scale = _as_vec2(raw.get("scale"), DEFAULT_SCALE, "scale", issues)
    return Keyframe(time=float(t), translation=translation,
                    rotation=rot, scale=scale)


def shortest_angle_delta(a: float, b: float) -> float:
    """从角度 a 到角度 b 的最短有向夹角（结果落在 (-180, 180]）。"""
    d = (b - a) % 360.0
    if d > 180.0:
        d -= 360.0
    return d


# ---------------------------------------------------------------- 加载与校验

def load_animation(path: str) -> Animation:
    """加载动画文件。

    文件级错误（不存在/不可读/顶层结构损坏）返回 load_error 非空的 Animation；
    单骨骼/单关键帧错误只记录在该骨骼的 issue 中，其他骨骼仍可预览（需求 4）。
    """
    if not os.path.exists(path):
        return _failed(f"文件不可用：路径不存在 {path}")
    if not os.path.isfile(path):
        return _failed(f"文件不可用：不是有效文件 {path}")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, PermissionError) as exc:
        return _failed(f"文件不可用：读取失败 {exc}")
    except json.JSONDecodeError as exc:
        return _failed(f"文件格式错误：JSON 解析失败（第 {exc.lineno} 行第 "
                       f"{exc.colno} 列）{exc.msg}")
    if not isinstance(data, dict):
        return _failed("文件格式错误：顶层必须是 JSON 对象")
    raw_bones = data.get("bones")
    if not isinstance(raw_bones, list):
        return _failed("文件格式错误：缺少 bones 数组")

    name = str(data.get("name", os.path.splitext(os.path.basename(path))[0]))
    explicit_duration = data.get("duration")
    explicit_duration = float(explicit_duration) if _is_number(explicit_duration) else None

    bones: List[Bone] = []
    total_keyframes = 0

    for raw_bone in raw_bones:
        if not isinstance(raw_bone, dict):
            # 无法确定骨骼名，跳过该条目
            continue
        bname = raw_bone.get("name")
        if not isinstance(bname, str) or not bname:
            bname = f"<未命名骨骼#{len(bones) + 1}>"
        length = raw_bone.get("length")
        length = float(length) if _is_number(length) else DEFAULT_BONE_LENGTH
        parent = raw_bone.get("parent")
        parent = parent if isinstance(parent, str) else None
        bone = Bone(name=bname, parent=parent, length=length, keyframes=[])

        raw_frames = raw_bone.get("keyframes", [])
        if not isinstance(raw_frames, list):
            bone.issue.messages.append("keyframes 不是数组，该骨骼按默认静止姿态处理")
            raw_frames = []
        total_keyframes += len(raw_frames)

        parsed: List[Keyframe] = []
        for raw_frame in raw_frames:
            kf = _parse_keyframe(raw_frame, bone.issue.messages)
            if kf is not None:
                parsed.append(kf)

        # 第一遍：确定总时长（显式 duration 优先，否则取最大关键帧时间）
        max_time = max((kf.time for kf in parsed), default=0.0)
        duration = explicit_duration if explicit_duration is not None else max_time

        # 第二遍：逐帧校验 重复时间 / 时间范围 / 时间倒序
        seen_times: set = set()
        last_time: Optional[float] = None
        for kf in parsed:
            t_repr = _format_time(kf.time)
            if kf.time in seen_times:
                reason = f"重复时间 {t_repr}s"
                bone.issue.messages.append(
                    f"骨骼「{bname}」存在重复时间关键帧：{t_repr}s，已跳过")
                bone.issue.skipped.append(SkippedFrame(kf.time, t_repr, reason))
                continue
            if kf.time < 0 or kf.time > duration:
                reason = (f"时间 {t_repr}s 为负" if kf.time < 0
                          else f"时间 {t_repr}s 超出总时长 {_format_time(duration)}s")
                bone.issue.messages.append(
                    f"骨骼「{bname}」关键帧 {reason}，已跳过")
                bone.issue.skipped.append(SkippedFrame(kf.time, t_repr, reason))
                continue
            if last_time is not None and kf.time < last_time:
                reason = f"时间倒序 {_format_time(last_time)}s -> {t_repr}s"
                bone.issue.messages.append(
                    f"骨骼「{bname}」关键帧时间倒序（{_format_time(last_time)}s 之后出现 "
                    f"{t_repr}s），已跳过")
                bone.issue.skipped.append(SkippedFrame(kf.time, t_repr, reason))
                continue
            seen_times.add(kf.time)
            last_time = kf.time
            bone.keyframes.append(kf)

        bone.times = [kf.time for kf in bone.keyframes]
        bones.append(bone)

    # 父骨骼名解析；未知父级按根骨骼处理并记录提示
    by_name: Dict[str, Bone] = {}
    for bone in bones:
        if bone.name in by_name:
            bone.issue.messages.append(
                f"骨骼名「{bone.name}」重复，后加载的同名骨骼在层级面板中仍会显示")
        by_name[bone.name] = bone
    for bone in bones:
        if bone.parent is not None and bone.parent not in by_name:
            bone.issue.messages.append(
                f"父骨骼「{bone.parent}」不存在，按根骨骼处理")
            bone.parent = None

    # 拓扑排序：保证父骨骼先于子骨骼（配合防护，杜绝环导致死循环）
    ordered = _topological_sort(bones, by_name)
    final_duration = explicit_duration if explicit_duration is not None else (
        max((b.times[-1] for b in ordered if b.times), default=0.0))
    final_duration = max(final_duration, 0.0)

    return Animation(name=name, duration=final_duration, bones=ordered,
                     by_name=by_name, total_keyframes=total_keyframes,
                     load_error=None, file_path=os.path.abspath(path))


def _failed(message: str) -> Animation:
    return Animation(name="(不可用)", duration=0.0, bones=[], by_name={},
                     total_keyframes=0, load_error=message)


def _topological_sort(bones: List[Bone], by_name: Dict[str, Bone]) -> List[Bone]:
    ordered: List[Bone] = []
    visited: set = set()
    visiting: set = set()

    def visit(bone: Bone) -> None:
        if bone.name in visited:
            return
        if bone.name in visiting:  # 环防护：断开环
            bone.issue.messages.append("检测到层级环，已将该骨骼按根骨骼处理")
            bone.parent = None
            return
        visiting.add(bone.name)
        if bone.parent and bone.parent in by_name:
            visit(by_name[bone.parent])
        visiting.discard(bone.name)
        visited.add(bone.name)
        ordered.append(bone)

    for bone in bones:
        visit(bone)
    return ordered


def _format_time(value: float) -> str:
    """时间展示：整数秒不带小数尾零。"""
    if abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    return f"{value:.3f}".rstrip("0").rstrip(".")


# ---------------------------------------------------------------- 插值

def _lerp(a: float, b: float, u: float) -> float:
    return a + (b - a) * u


def local_pose(bone: Bone, time: float) -> Tuple[Tuple[float, float], float,
                                                  Tuple[float, float]]:
    """计算骨骼在 time 处的局部姿态（平移、旋转角、缩放）。

    - 平移/缩放线性插值；旋转走最短路径角度插值（需求 3）
    - 无关键帧：返回默认静止姿态
    - 时间超出首末关键帧：保持最近关键帧值（clamp）
    """
    frames = bone.keyframes
    if not frames:
        return DEFAULT_TRANSLATION, DEFAULT_ROTATION, DEFAULT_SCALE

    times = bone.times
    if time <= times[0]:
        kf = frames[0]
        return kf.translation, kf.rotation % 360.0, kf.scale
    if time >= times[-1]:
        kf = frames[-1]
        return kf.translation, kf.rotation % 360.0, kf.scale

    idx = bisect_right(times, time) - 1
    k0, k1 = frames[idx], frames[idx + 1]
    span = k1.time - k0.time
    u = 0.0 if span <= 0 else (time - k0.time) / span

    translation = (_lerp(k0.translation[0], k1.translation[0], u),
                   _lerp(k0.translation[1], k1.translation[1], u))
    scale = (_lerp(k0.scale[0], k1.scale[0], u),
             _lerp(k0.scale[1], k1.scale[1], u))
    rotation = (k0.rotation + shortest_angle_delta(k0.rotation, k1.rotation) * u) % 360.0
    return translation, rotation, scale


def compose_pose(anim: Animation, time: float) -> None:
    """按层级在 time 处合成世界姿态，结果写回每根骨骼的 origin/tip 等字段。"""
    for bone in anim.bones:
        trans, rot, scale = local_pose(bone, time)
        if bone.parent is None:
            origin = trans
            world_rot = rot
            world_scale = scale
        else:
            parent = anim.by_name[bone.parent]
            rad = math.radians(parent.world_rotation)
            cos_r, sin_r = math.cos(rad), math.sin(rad)
            # 子骨骼局部平移先受父级缩放，再随父级旋转，最后叠加父级原点
            local_x = trans[0] * parent.world_scale[0]
            local_y = trans[1] * parent.world_scale[1]
            origin = (
                parent.origin[0] + local_x * cos_r - local_y * sin_r,
                parent.origin[1] + local_x * sin_r + local_y * cos_r,
            )
            world_rot = parent.world_rotation + rot
            world_scale = (parent.world_scale[0] * scale[0],
                           parent.world_scale[1] * scale[1])
        tip_rad = math.radians(world_rot)
        tip = (
            origin[0] + math.cos(tip_rad) * bone.length * world_scale[0],
            origin[1] + math.sin(tip_rad) * bone.length * world_scale[1],
        )
        bone.origin = origin
        bone.world_rotation = world_rot % 360.0
        bone.world_scale = world_scale
        bone.tip = tip


# ---------------------------------------------------------------- 报告导出

def _round2(value: float) -> float:
    return round(float(value) + 0.0, 2)  # 加 0.0 避免 -0.0 残留


def export_pose_report(anim: Animation, time: float, out_path: str) -> str:
    """导出当前时间点姿态报告 JSON（需求 7），坐标/角度/缩放均保留两位小数。"""
    compose_pose(anim, time)
    report = {
        "file": anim.file_path,
        "animation": anim.name,
        "time": _round2(time),
        "bones": [
            {
                "name": bone.name,
                "parent": bone.parent,
                "world": {
                    "x": _round2(bone.origin[0]),
                    "y": _round2(bone.origin[1]),
                    "rotation": _round2(bone.world_rotation),
                    "scale_x": _round2(bone.world_scale[0]),
                    "scale_y": _round2(bone.world_scale[1]),
                },
                "tip": {
                    "x": _round2(bone.tip[0]),
                    "y": _round2(bone.tip[1]),
                },
            }
            for bone in anim.bones
        ],
    }
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    return os.path.abspath(out_path)
