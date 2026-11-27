"""模型轨迹关键帧生成。

关键帧时间戳按固定帧率等间隔分布；每个关键帧保存逐顶点变换后位置、
刚性变换矩阵、时间戳与缓动进度值。同时暴露标量轨迹供平滑重采样复用。
"""

import numpy as np

from .constraints import _rotate_around_axis
from .easing import eased_progress
from .enums import ActionType, Direction, Easing, DIRECTION_PARTS
from .geometry import Model


def frame_timestamps(period: float, fps: int) -> np.ndarray:
    """等间隔时间戳，含首尾两帧。"""
    count = max(int(round(period * fps)) + 1, 2)
    return np.linspace(0.0, float(period), count)


def scalar_trajectory(parsed: dict, adjusted_amplitude: float,
                      timestamps: np.ndarray) -> np.ndarray:
    """生成动作的一维标量轨迹（角度/距离/外扩量），与动作类型语义一致。

    ROTATE   : 0 -> 幅度（单向，符号决定方向）
    SWING    : 0 -> +幅度 -> 0（一个完整来回，角度绝对值）
    TRANSLATE: 0 -> 幅度（单向位移，符号决定轴向正负）
    EXPAND   : 0 -> 幅度（外扩距离）
    """
    action_type = ActionType(parsed["action_type"])
    easing = Easing(parsed["easing"])
    period = float(parsed["period"])
    values = []
    for t in timestamps:
        p = float(np.clip(t / period, 0.0, 1.0))
        e = eased_progress(p, easing)
        if action_type == ActionType.SWING:
            # 缓动曲线决定单程节奏，正弦空间完成往返
            values.append(adjusted_amplitude * np.sin(np.pi * p))
        else:
            values.append(adjusted_amplitude * e)
    return np.asarray(values, dtype=np.float64)


def _rigid_matrix(action_type: ActionType, axis: str, scalar: float) -> np.ndarray:
    matrix = np.eye(4)
    if action_type in (ActionType.ROTATE, ActionType.SWING):
        angle = np.radians(scalar)
        c, s = np.cos(angle), np.sin(angle)
        if axis == "X":
            rotation = np.array([[1, 0, 0], [0, c, -s], [0, s, c]])
        elif axis == "Y":
            rotation = np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]])
        else:
            rotation = np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]])
        matrix[:3, :3] = rotation
    else:  # TRANSLATE
        vector = np.zeros(3)
        vector[{"X": 0, "Y": 1, "Z": 2}[axis]] = scalar
        matrix[:3, 3] = vector
    return matrix


def _apply_expand(model: Model, amount: float) -> np.ndarray:
    """按材质分区沿顶点法向外扩，每个分区独立移动其顶点子集。"""
    moved = model.vertices.copy()
    for group in model.groups.values():
        indices = np.asarray(group.vertex_indices, dtype=np.int64)
        if indices.size == 0:
            continue
        moved[indices] = model.vertices[indices] + amount * model.vertex_normals[indices]
    return moved


def build_trajectory(parsed: dict, constraint_record: dict,
                     model: Model, fps: int) -> dict:
    """生成单个动作的模型轨迹关键帧序列。"""
    direction = Direction(parsed["direction"])
    action_type, axis = DIRECTION_PARTS[direction]
    easing = Easing(parsed["easing"])
    period = float(parsed["period"])
    adjusted = float(constraint_record["amplitude_adjusted"])

    timestamps = frame_timestamps(period, fps)
    scalars = scalar_trajectory(parsed, adjusted, timestamps)

    keyframes = []
    relative = model.vertices - model.center
    for frame_index, (t, scalar) in enumerate(zip(timestamps, scalars)):
        progress = float(np.clip(t / period, 0.0, 1.0))
        ease_value = float(eased_progress(progress, easing))
        if action_type == ActionType.EXPAND:
            positions = _apply_expand(model, scalar)
            matrix = np.eye(4)  # 非刚性变形，单位矩阵占位
        elif action_type in (ActionType.ROTATE, ActionType.SWING):
            rotated = _rotate_around_axis(relative, axis, np.radians(scalar))
            positions = rotated + model.center
            matrix = _rigid_matrix(action_type, axis, scalar)
        else:  # TRANSLATE
            positions = model.vertices.copy()
            matrix = _rigid_matrix(action_type, axis, scalar)
            positions += matrix[:3, 3]
        keyframes.append({
            "frame": frame_index,
            "timestamp": float(t),
            "ease_progress": ease_value,
            "scalar": float(scalar),
            "transform_matrix": matrix,
            "positions": positions,
        })

    return {
        "index": parsed["index"],
        "action_type": action_type.value,
        "direction": direction.value,
        "fps": fps,
        "period": period,
        "timestamps": timestamps,
        "scalars": scalars,
        "keyframes": keyframes,
    }


def rebuild_trajectory_from_scalars(parsed: dict, model: Model,
                                    timestamps: np.ndarray,
                                    scalars: np.ndarray) -> list:
    """平滑重采样后，依据修正过的标量轨迹重建关键帧（复用同一变换规则）。"""
    direction = Direction(parsed["direction"])
    action_type, axis = DIRECTION_PARTS[direction]
    easing = Easing(parsed["easing"])
    period = float(parsed["period"])
    relative = model.vertices - model.center

    rebuilt = []
    for frame_index, (t, scalar) in enumerate(zip(timestamps, scalars)):
        progress = float(np.clip(t / period, 0.0, 1.0))
        ease_value = float(eased_progress(progress, easing))
        if action_type == ActionType.EXPAND:
            positions = _apply_expand(model, scalar)
            matrix = np.eye(4)
        elif action_type in (ActionType.ROTATE, ActionType.SWING):
            rotated = _rotate_around_axis(relative, axis, np.radians(scalar))
            positions = rotated + model.center
            matrix = _rigid_matrix(action_type, axis, scalar)
        else:
            positions = model.vertices.copy()
            matrix = _rigid_matrix(action_type, axis, scalar)
            positions += matrix[:3, 3]
        rebuilt.append({
            "frame": frame_index,
            "timestamp": float(t),
            "ease_progress": ease_value,
            "scalar": float(scalar),
            "transform_matrix": matrix,
            "positions": positions,
        })
    return rebuilt
