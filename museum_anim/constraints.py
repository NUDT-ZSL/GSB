"""展柜边界约束求解：包围盒可行性检查与幅度等比压缩。"""

import numpy as np

from .enums import ActionType, ConstraintStatus, Direction, DIRECTION_PARTS
from .geometry import Model

AXIS_INDEX = {"X": 0, "Y": 1, "Z": 2}


class CaseConfig:
    """展柜矩形空间范围（米）。"""

    def __init__(self, bounds: dict):
        self.bounds = bounds
        self.min = np.asarray(bounds["min"], dtype=np.float64)
        self.max = np.asarray(bounds["max"], dtype=np.float64)
        if np.any(self.max <= self.min):
            raise ValueError("展柜边界 max 必须在各轴上严格大于 min")

    @property
    def volume(self) -> float:
        return float(np.prod(self.max - self.min))


def validate_model_inside_case(model: Model, case: CaseConfig) -> None:
    """初始姿态的模型必须完全位于展柜内，否则输入非法。"""
    if np.any(model.aabb_min < case.min - 1e-9) or np.any(model.aabb_max > case.max + 1e-9):
        raise ValueError("文物包围盒超出展柜边界，请检查模型坐标或展柜配置")


def boundary_occupancy(model: Model, case: CaseConfig) -> float:
    """模型 AABB 体积占展柜空间体积的比例。"""
    model_volume = float(np.prod(np.maximum(model.aabb_max - model.aabb_min, 0.0)))
    return model_volume / case.volume


def _rotated_points(model: Model, axis: str, angle_rad: float) -> np.ndarray:
    vec = model.vertices - model.center
    rotated = _rotate_around_axis(vec, axis, angle_rad)
    return rotated + model.center


def _rotate_around_axis(points: np.ndarray, axis: str, angle_rad: float) -> np.ndarray:
    c, s = np.cos(angle_rad), np.sin(angle_rad)
    if axis == "X":
        rot = np.array([[1, 0, 0], [0, c, -s], [0, s, c]])
    elif axis == "Y":
        rot = np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]])
    else:
        rot = np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]])
    return points @ rot.T


def _points_inside(points: np.ndarray, case: CaseConfig, tol: float = 1e-9) -> bool:
    mn = points.min(axis=0)
    mx = points.max(axis=0)
    return bool(np.all(mn >= case.min - tol) and np.all(mx <= case.max + tol))


def _max_feasible_rotation(model: Model, case: CaseConfig, axis: str) -> float:
    """扫描 + 二分求绕某轴旋转不越界的最大角度（弧度，范围 [0, 2pi]）。

    旋转可行性在角度上不一定单调（可能先越界后回到界内），因此先在整圈
    上密集采样找到首个越界点，再在其与上一个可行点之间二分细化。
    """
    def feasible(angle: float) -> bool:
        return _points_inside(_rotated_points(model, axis, angle), case)

    samples = np.linspace(0.0, 2.0 * np.pi, 241)
    first_violation = None
    for idx in range(1, len(samples)):
        if not feasible(samples[idx]):
            first_violation = idx
            break
    if first_violation is None:
        return 2.0 * np.pi  # 完整一周均可行
    low, high = samples[first_violation - 1], samples[first_violation]
    for _ in range(40):
        mid = (low + high) / 2.0
        if feasible(mid):
            low = mid
        else:
            high = mid
    return low


def _max_feasible_translation(model: Model, case: CaseConfig, axis: str) -> float:
    """沿某轴的最大可平移距离（米，取正负两侧最小间隙）。"""
    idx = AXIS_INDEX[axis]
    positive_gap = float(case.max[idx] - model.aabb_max[idx])
    negative_gap = float(model.aabb_min[idx] - case.min[idx])
    return max(min(positive_gap, negative_gap), 0.0)


def _max_feasible_expand(model: Model, case: CaseConfig) -> float:
    """展开沿法线外扩：用各顶点方向与展柜壁的最小距离限制外扩量。"""
    directions = model.vertex_normals
    limits = []
    for i in range(model.vertices.shape[0]):
        for axis_idx in range(3):
            component = directions[i, axis_idx]
            if component > 1e-9:
                limits.append((case.max[axis_idx] - model.vertices[i, axis_idx]) / component)
            elif component < -1e-9:
                limits.append((model.vertices[i, axis_idx] - case.min[axis_idx]) / -component)
    return max(min(limits), 0.0) if limits else 0.0


def solve_action_constraint(parsed: dict, model: Model, case: CaseConfig) -> dict:
    """对单条解析后的动作求解实际可用运动范围。

    返回约束求解记录：原始幅度、约束后幅度、状态、压缩比例、边界间隙。
    """
    direction = Direction(parsed["direction"])
    action_type, axis = DIRECTION_PARTS[direction]
    requested = float(parsed["amplitude"])
    magnitude = abs(requested)

    if action_type in (ActionType.ROTATE, ActionType.SWING):
        max_angle_rad = _max_feasible_rotation(model, case, axis)
        max_amplitude = float(np.degrees(max_angle_rad))
        unit = "degree"
        gaps = _axis_gaps(model, case)
    elif action_type == ActionType.TRANSLATE:
        max_amplitude = _max_feasible_translation(model, case, axis)
        unit = "meter"
        gaps = _axis_gaps(model, case)
    else:  # EXPAND
        max_amplitude = _max_feasible_expand(model, case)
        unit = "meter"
        gaps = _axis_gaps(model, case)

    if magnitude <= max_amplitude + 1e-9:
        status = ConstraintStatus.WITHIN_BOUNDS
        adjusted = requested
        ratio = 1.0
    else:
        status = ConstraintStatus.CONSTRAINED
        ratio = max_amplitude / magnitude if magnitude > 0 else 1.0
        adjusted = np.sign(requested) * max_amplitude if requested != 0 else max_amplitude

    return {
        "index": parsed["index"],
        "action_type": action_type.value,
        "direction": direction.value,
        "unit": unit,
        "amplitude_requested": round(float(requested), 6),
        "amplitude_adjusted": round(float(adjusted), 6),
        "max_feasible_amplitude": round(float(max_amplitude), 6),
        "compression_ratio": round(float(ratio), 6),
        "status": status.value,
        "boundary_gaps": {k: round(v, 6) for k, v in gaps.items()},
    }


def _axis_gaps(model: Model, case: CaseConfig) -> dict:
    return {
        "x": round(float(min(case.max[0] - model.aabb_max[0],
                             model.aabb_min[0] - case.min[0])), 6),
        "y": round(float(min(case.max[1] - model.aabb_max[1],
                             model.aabb_min[1] - case.min[1])), 6),
        "z": round(float(min(case.max[2] - model.aabb_max[2],
                             model.aabb_min[2] - case.min[2])), 6),
    }
