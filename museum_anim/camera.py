"""同步相机环绕动画。

相机在展柜外部沿以模型中心为圆心的圆弧运动：半径由视角约束与画面比例
计算，高度/俯仰满足展陈视角要求，并逐帧校验视锥不与模型包围盒相交。
"""

import numpy as np

from .geometry import Model


class CameraConfig:
    def __init__(self, config: dict):
        self.fov_deg = float(config.get("fov_deg", 45.0))
        self.aspect = float(config.get("aspect_ratio", config.get("aspect", 16.0 / 9.0)))
        self.elevation_deg = float(config.get("elevation_deg", 20.0))
        self.safety_margin = float(config.get("safety_margin", 0.15))
        self.orbit_deg = float(config.get("orbit_deg", 360.0))
        self.near = float(config.get("near", 0.05))
        self.far = float(config.get("far", 100.0))
        self.look_at_offset = np.asarray(
            config.get("look_at_offset", [0.0, 0.0, 0.0]), dtype=np.float64
        )


def _framing_radius(model: Model, cam: CameraConfig) -> float:
    """让模型包围盒完整入画所需的最小半径（同时考虑水平/垂直两个视场角）。"""
    fov_v = np.radians(cam.fov_deg)
    fov_h = 2.0 * np.arctan(np.tan(fov_v / 2.0) * cam.aspect)
    extent = float(np.max(model.half_extents))
    radius_v = extent / np.tan(fov_v / 2.0)
    radius_h = extent / np.tan(fov_h / 2.0)
    return max(radius_v, radius_h) * (1.0 + cam.safety_margin)


def rotation_matrix_to_quaternion(matrix: np.ndarray) -> np.ndarray:
    """3x3 旋转矩阵 -> 四元数 [w, x, y, z]（按对角元分支，稳定且确定性）。"""
    m = matrix[:3, :3]
    trace = np.trace(m)
    if trace > 0.0:
        s = np.sqrt(trace + 1.0) * 2.0
        w = 0.25 * s
        x = (m[2, 1] - m[1, 2]) / s
        y = (m[0, 2] - m[2, 0]) / s
        z = (m[1, 0] - m[0, 1]) / s
    elif m[0, 0] > m[1, 1] and m[0, 0] > m[2, 2]:
        s = np.sqrt(1.0 + m[0, 0] - m[1, 1] - m[2, 2]) * 2.0
        w = (m[2, 1] - m[1, 2]) / s
        x = 0.25 * s
        y = (m[1, 0] + m[0, 1]) / s
        z = (m[0, 2] + m[2, 0]) / s
    elif m[1, 1] > m[2, 2]:
        s = np.sqrt(1.0 + m[1, 1] - m[0, 0] - m[2, 2]) * 2.0
        w = (m[0, 2] - m[2, 0]) / s
        x = (m[1, 0] + m[0, 1]) / s
        y = 0.25 * s
        z = (m[2, 1] + m[1, 2]) / s
    else:
        s = np.sqrt(1.0 + m[2, 2] - m[0, 0] - m[1, 1]) * 2.0
        w = (m[1, 0] - m[0, 1]) / s
        x = (m[0, 2] + m[2, 0]) / s
        y = (m[2, 1] + m[1, 2]) / s
        z = 0.25 * s
    quat = np.array([w, x, y, z])
    return quat / np.linalg.norm(quat)


def _view_rotation(forward: np.ndarray, world_up: np.ndarray) -> np.ndarray:
    """构造相机朝向旋转矩阵（相机局部 -Z 指向目标，+Y 为上）。"""
    z_axis = -forward / np.linalg.norm(forward)
    x_axis = np.cross(world_up, z_axis)
    x_axis /= np.linalg.norm(x_axis)
    y_axis = np.cross(z_axis, x_axis)
    return np.column_stack([x_axis, y_axis, z_axis])


def frustum_intersects_aabb(position, rotation, cam, aabb_min, aabb_max):
    """视锥与 AABB 是否相交；任一平面完全分离即不相交（视锥不碰包围盒）。

    Gribb-Hartmann 平面提取：相机局部空间（光轴 -Z）构造六平面，
    再变换到世界空间，对 AABB 做正顶点（p-vertex）测试。
    """
    tan_v = np.tan(np.radians(cam.fov_deg) / 2.0)
    tan_h = tan_v * cam.aspect
    near, far = cam.near, cam.far
    local_planes = [
        (np.array([0.0, 0.0, 1.0]), near),
        (np.array([0.0, 0.0, -1.0]), -far),
        (np.array([1.0, 0.0, -tan_h]), 0.0),
        (np.array([-1.0, 0.0, -tan_h]), 0.0),
        (np.array([0.0, 1.0, -tan_v]), 0.0),
        (np.array([0.0, -1.0, -tan_v]), 0.0),
    ]
    center = (aabb_min + aabb_max) / 2.0
    half = (aabb_max - aabb_min) / 2.0
    for normal_local, constant in local_planes:
        normal_world = rotation @ normal_local
        d_world = constant - normal_world @ position
        max_projection = center @ normal_world + half @ np.abs(normal_world) + d_world
        if max_projection < -1e-9:
            return False
    return True


def build_camera_trajectory(model, case, cam, timestamps, action_index):
    """生成与模型关键帧时间戳完全对齐的相机环绕轨迹。"""
    target = model.center + cam.look_at_offset
    base_radius = _framing_radius(model, cam)

    case_half = float(np.max(case.max - case.min)) / 2.0
    elevation = np.radians(cam.elevation_deg)
    outside_radius = case_half / max(np.cos(elevation), 1e-3) * 1.05
    radius = max(base_radius, outside_radius)
    horizontal_radius = radius * np.cos(elevation)
    height = radius * np.sin(elevation)

    span = float(timestamps[-1] - timestamps[0]) if len(timestamps) > 1 else 1.0
    orbit = np.radians(cam.orbit_deg)
    world_up = np.array([0.0, 0.0, 1.0])

    keyframes = []
    clear_flags = []
    for frame_index, t in enumerate(timestamps):
        progress = 0.0 if span == 0 else float((t - timestamps[0]) / span)
        angle = orbit * progress
        position = np.array([
            target[0] + horizontal_radius * np.cos(angle),
            target[1] + horizontal_radius * np.sin(angle),
            target[2] + height,
        ])
        forward = target - position
        rotation = _view_rotation(forward, world_up)
        quaternion = rotation_matrix_to_quaternion(rotation)
        intersects = frustum_intersects_aabb(
            position, rotation, cam, model.aabb_min, model.aabb_max
        )
        clear_flags.append(not intersects)
        keyframes.append({
            "frame": frame_index,
            "timestamp": float(t),
            "position": position,
            "orientation_quaternion": quaternion,
            "fov_deg": cam.fov_deg,
            "frustum_clear": not intersects,
        })

    return {
        "action_index": action_index,
        "radius": float(radius),
        "height": float(height),
        "elevation_deg": cam.elevation_deg,
        "fov_deg": cam.fov_deg,
        "aspect_ratio": cam.aspect,
        "keyframes": keyframes,
        "all_frames_clear": bool(all(clear_flags)),
    }
