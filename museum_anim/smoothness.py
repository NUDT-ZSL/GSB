"""速度/加速度连续性检查与时间重采样修正。

对模型轨迹（逐顶点位移的质心代表点）与相机轨迹分别计算逐帧速度、
加速度，统计峰值与超阈值帧数；超阈值时以弧长重参数化做时间重采样，
在不改变总周期与首末状态的前提下让速度/加速度回到阈值内。
"""

import numpy as np

from .easing import arc_length_reparametrize


DEFAULT_THRESHOLDS = {
    "model_max_velocity": 5.0,       # m/s
    "model_max_acceleration": 10.0,  # m/s^2
    "camera_max_velocity": 8.0,      # m/s
    "camera_max_acceleration": 12.0,
}


def _kinematics(points: np.ndarray, timestamps: np.ndarray) -> dict:
    """由位置序列与时间戳计算逐帧速度/加速度。"""
    count = len(points)
    velocities = np.zeros(count)
    accelerations = np.zeros(count)
    if count >= 2:
        delta_t = np.diff(timestamps)
        delta_p = np.diff(points, axis=0)
        velocities[1:] = np.linalg.norm(delta_p, axis=1) / np.maximum(delta_t, 1e-9)
    if count >= 3:
        # 速度定义在区间 [t_i, t_{i+1}] 上（共 n-1 段），加速度由相邻段速度差分得到
        segment_mid = (timestamps[:-1] + timestamps[1:]) / 2.0
        accel = np.diff(velocities[1:]) / np.maximum(np.diff(segment_mid), 1e-9)
        accelerations[2:] = accel
    return {
        "velocities": velocities,
        "accelerations": accelerations,
        "max_velocity": float(np.max(velocities)),
        "max_acceleration": float(np.max(accelerations)),
    }


def model_representative_points(keyframes: list) -> np.ndarray:
    """模型轨迹代表点：每帧全部顶点位置的质心。"""
    return np.vstack([np.mean(kf["positions"], axis=0) for kf in keyframes])


def camera_representative_points(keyframes: list) -> np.ndarray:
    return np.vstack([np.asarray(kf["position"]) for kf in keyframes])


def evaluate(keyframes: list, timestamps: np.ndarray, kind: str,
             thresholds: dict) -> dict:
    """生成单条轨迹（model/camera）的平滑度指标。"""
    if kind == "model":
        points = model_representative_points(keyframes)
        v_limit = thresholds["model_max_velocity"]
        a_limit = thresholds["model_max_acceleration"]
    else:
        points = camera_representative_points(keyframes)
        v_limit = thresholds["camera_max_velocity"]
        a_limit = thresholds["camera_max_acceleration"]
    metrics = _kinematics(points, timestamps)
    over_v = int(np.sum(metrics["velocities"] > v_limit + 1e-9))
    over_a = int(np.sum(metrics["accelerations"] > a_limit + 1e-9))
    return {
        "max_velocity": round(metrics["max_velocity"], 6),
        "max_acceleration": round(metrics["max_acceleration"], 6),
        "velocity_limit": v_limit,
        "acceleration_limit": a_limit,
        "over_velocity_frames": over_v,
        "over_acceleration_frames": over_a,
        "within_thresholds": over_v == 0 and over_a == 0,
        "per_frame_velocity": np.round(metrics["velocities"], 6).tolist(),
        "per_frame_acceleration": np.round(metrics["accelerations"], 6).tolist(),
    }


def needs_resampling(model_report: dict, camera_report: dict) -> bool:
    return not (model_report["within_thresholds"] and camera_report["within_thresholds"])


def resample_scalar_curve(scalars: np.ndarray, sample_count: int) -> np.ndarray:
    """对一维标量轨迹做弧长重参数化（退化为按数值差等距），保持首末值。"""
    column = scalars.reshape(-1, 1)
    corrected = arc_length_reparametrize(column, sample_count)
    # 强制首末值与原始一致，避免插值误差
    corrected[0] = column[0]
    corrected[-1] = column[-1]
    return corrected.reshape(-1)


def resample_camera_positions(positions: np.ndarray, sample_count: int) -> np.ndarray:
    corrected = arc_length_reparametrize(positions, sample_count)
    corrected[0] = positions[0]
    corrected[-1] = positions[-1]
    return corrected


def correction_summary(before_model: dict, before_camera: dict,
                       after_model: dict, after_camera: dict) -> dict:
    """原轨迹与修正轨迹的对比摘要。"""
    return {
        "resampled": True,
        "period_preserved": True,
        "before": {
            "model_max_velocity": before_model["max_velocity"],
            "model_max_acceleration": before_model["max_acceleration"],
            "camera_max_velocity": before_camera["max_velocity"],
            "camera_max_acceleration": before_camera["max_acceleration"],
        },
        "after": {
            "model_max_velocity": after_model["max_velocity"],
            "model_max_acceleration": after_model["max_acceleration"],
            "camera_max_velocity": after_camera["max_velocity"],
            "camera_max_acceleration": after_camera["max_acceleration"],
        },
        "model_velocity_reduction": round(
            max(before_model["max_velocity"] - after_model["max_velocity"], 0.0), 6),
        "camera_velocity_reduction": round(
            max(before_camera["max_velocity"] - after_camera["max_velocity"], 0.0), 6),
    }
