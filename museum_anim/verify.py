"""独立动作语义核对。

重要约束：本模块只读取最终关键帧序列（顶点位置/相机位姿/时间戳），
不复用解析或生成阶段的任何中间参数，所有指标均从关键帧重新计算。
阈值：方向偏差 >15°、幅度相对误差 >5%、周期相对误差 >2% 即标记超阈值。
"""

import numpy as np

from .enums import ActionType, Direction

DIRECTION_THRESHOLD_DEG = 15.0
AMPLITUDE_REL_TOL = 0.05
PERIOD_REL_TOL = 0.02


def _frame_centers(keyframes: list) -> np.ndarray:
    return np.vstack([np.mean(kf["positions"], axis=0) for kf in keyframes])


def _displacements(keyframes: list) -> np.ndarray:
    centers = _frame_centers(keyframes)
    return centers - centers[0]


def _dominant_rotation_axis(keyframes: list) -> np.ndarray:
    """从相邻帧质心位移叉积反算旋转主轴。"""
    centers = _frame_centers(keyframes)
    delta = np.diff(centers, axis=0)
    accum = np.zeros(3)
    for i in range(len(delta) - 1):
        cross = np.cross(delta[i], delta[i + 1])
        norm = np.linalg.norm(cross)
        if norm > 1e-12:
            accum += cross / norm
    if np.linalg.norm(accum) < 1e-12:
        # 退化情形（采样点近似共面位移），用相对初始中心的主协方差轴
        relative = centers - centers[0]
        _, _, vh = np.linalg.svd(relative, full_matrices=False)
        accum = vh[np.argmin(np.abs(vh).sum(axis=1))]
    return accum / np.linalg.norm(accum)


def _angle_between_deg(vec_a: np.ndarray, vec_b: np.ndarray) -> float:
    cosine = float(np.clip(np.dot(vec_a, vec_b), -1.0, 1.0))
    return float(np.degrees(np.arccos(cosine)))


def _undirected_axis_error(measured: np.ndarray, expected: np.ndarray) -> float:
    """旋转/平移主轴是无向的，取 v 与 -v 中较小的夹角。"""
    return min(_angle_between_deg(measured, expected),
               _angle_between_deg(-measured, expected))


def _expected_axis_vector(direction: Direction) -> np.ndarray:
    mapping = {
        Direction.ROT_X: (1, 0, 0), Direction.SWING_X: (1, 0, 0),
        Direction.ROT_Y: (0, 1, 0), Direction.SWING_Y: (0, 1, 0),
        Direction.ROT_Z: (0, 0, 1), Direction.SWING_Z: (0, 0, 1),
        Direction.MOVE_X: (1, 0, 0), Direction.MOVE_Y: (0, 1, 0),
        Direction.MOVE_Z: (0, 0, 1),
    }
    return np.asarray(mapping[direction], dtype=np.float64)


def _measured_period(keyframes: list) -> float:
    timestamps = np.asarray([kf["timestamp"] for kf in keyframes])
    return float(timestamps[-1] - timestamps[0])


def _rotation_amplitude_deg(keyframes: list, axis: np.ndarray) -> float:
    """把参考顶点投影到垂直于旋转轴的平面，用解绕后的有符号角度求总行程。

    直接取首尾夹角会把 360° 旋转误判为 0°，因此逐帧计算有符号增量并累积，
    幅度取累积角度的最大绝对行程（同时适用于整周旋转与来回摆动）。
    """
    first = keyframes[0]["positions"]
    reference_vertex = int(np.argmax(np.linalg.norm(first - np.mean(first, axis=0), axis=1)))
    center0 = np.mean(first, axis=0)
    ref0 = first[reference_vertex] - center0
    ref0 = ref0 - axis * np.dot(ref0, axis)
    ref0 /= np.linalg.norm(ref0)
    cumulative = 0.0
    peak = 0.0
    previous_vec = ref0
    for kf in keyframes:
        center = np.mean(kf["positions"], axis=0)
        vec = kf["positions"][reference_vertex] - center
        vec = vec - axis * np.dot(vec, axis)
        vec /= np.linalg.norm(vec)
        dot = float(np.clip(np.dot(previous_vec, vec), -1.0, 1.0))
        signed_step = np.degrees(np.arccos(dot))
        if np.dot(axis, np.cross(previous_vec, vec)) < 0.0:
            signed_step = -signed_step
        cumulative += signed_step
        peak = max(peak, abs(cumulative))
        previous_vec = vec
    return float(peak)


def _translation_amplitude(keyframes: list) -> np.ndarray:
    centers = _frame_centers(keyframes)
    return centers[-1] - centers[0]


def _expand_amplitude(keyframes: list) -> float:
    first = keyframes[0]["positions"]
    last = keyframes[-1]["positions"]
    displacement = np.linalg.norm(last - first, axis=1)
    return float(np.max(displacement))


def verify_action(parsed: dict, constraint_record: dict, keyframes: list,
                  camera_trajectory: dict) -> dict:
    """对单个动作执行独立核对，返回期望值/实测值与超阈值标记。"""
    action_type = ActionType(parsed["action_type"])
    direction = Direction(parsed["direction"])
    expected_amplitude = abs(float(constraint_record["amplitude_adjusted"]))
    expected_period = float(parsed["period"])
    measured_period = _measured_period(keyframes)

    result = {
        "index": parsed["index"],
        "raw_text": parsed["raw_text"],
        "expected": {
            "direction": direction.value,
            "amplitude": round(expected_amplitude, 6),
            "period": round(expected_period, 6),
        },
        "measured": {},
        "violations": [],
        "passed": True,
    }

    if action_type in (ActionType.ROTATE, ActionType.SWING):
        measured_axis = _dominant_rotation_axis(keyframes)
        expected_axis = _expected_axis_vector(direction)
        axis_error = _undirected_axis_error(measured_axis, expected_axis)
        measured_amplitude = _rotation_amplitude_deg(keyframes, measured_axis)
        result["measured"] = {
            "dominant_axis": np.round(measured_axis, 6).tolist(),
            "axis_error_deg": round(axis_error, 6),
            "amplitude": round(measured_amplitude, 6),
            "period": round(measured_period, 6),
        }
        if axis_error > DIRECTION_THRESHOLD_DEG:
            result["violations"].append("DIRECTION")
    elif action_type == ActionType.TRANSLATE:
        vector = _translation_amplitude(keyframes)
        measured_axis = vector / max(np.linalg.norm(vector), 1e-12)
        expected_axis = _expected_axis_vector(direction)
        axis_error = _undirected_axis_error(measured_axis, expected_axis)
        measured_amplitude = float(np.linalg.norm(vector))
        result["measured"] = {
            "dominant_axis": np.round(measured_axis, 6).tolist(),
            "axis_error_deg": round(axis_error, 6),
            "amplitude": round(measured_amplitude, 6),
            "period": round(measured_period, 6),
        }
        if axis_error > DIRECTION_THRESHOLD_DEG:
            result["violations"].append("DIRECTION")
    else:  # EXPAND
        measured_amplitude = _expand_amplitude(keyframes)
        result["measured"] = {
            "dominant_axis": "EXPAND",
            "axis_error_deg": 0.0,
            "amplitude": round(measured_amplitude, 6),
            "period": round(measured_period, 6),
        }

    # 幅度与周期相对误差
    if expected_amplitude > 1e-9:
        amplitude_error = abs(measured_amplitude - expected_amplitude) / expected_amplitude
    else:
        amplitude_error = 0.0
    period_error = (abs(measured_period - expected_period) / expected_period
                    if expected_period > 1e-9 else 0.0)
    result["measured"]["amplitude_relative_error"] = round(amplitude_error, 6)
    result["measured"]["period_relative_error"] = round(period_error, 6)
    if amplitude_error > AMPLITUDE_REL_TOL:
        result["violations"].append("AMPLITUDE")
    if period_error > PERIOD_REL_TOL:
        result["violations"].append("PERIOD")

    result["camera_all_frames_clear"] = camera_trajectory["all_frames_clear"]
    if not camera_trajectory["all_frames_clear"]:
        result["violations"].append("CAMERA_FRUSTUM")
    result["passed"] = len(result["violations"]) == 0
    return result
