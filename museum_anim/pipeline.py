"""端到端动画流水线与确定性 JSON 序列化。"""

import numpy as np

from .camera import CameraConfig, build_camera_trajectory
from .constraints import CaseConfig, boundary_occupancy, solve_action_constraint, validate_model_inside_case
from .geometry import Model, model_from_arrays, parse_obj
from .parser import UnparsedEntry, parse_script
from .smoothness import (
    DEFAULT_THRESHOLDS,
    camera_representative_points,
    correction_summary,
    evaluate,
    needs_resampling,
    resample_camera_positions,
    resample_scalar_curve,
)
from .trajectory import build_trajectory, rebuild_trajectory_from_scalars
from .verify import verify_action

PRECISION = 6


class PipelineError(ValueError):
    """输入或配置层面的业务错误。"""


class UnparsedAction(PipelineError):
    def __init__(self, index: int, text: str, reason: str):
        super().__init__(reason)
        self.code = "E_UNPARSED"
        self.index = index
        self.text = text
        self.reason = reason


def _round(value, precision: int = PRECISION):
    if isinstance(value, (np.floating, float)):
        return round(float(value), precision)
    if isinstance(value, (np.integer, int)):
        return int(value)
    if isinstance(value, np.ndarray):
        return np.round(value.astype(np.float64), precision).tolist()
    if isinstance(value, dict):
        return {k: _round(v, precision) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_round(v, precision) for v in value]
    return value


def _serialize_keyframe(keyframe: dict) -> dict:
    return {
        "frame": int(keyframe["frame"]),
        "timestamp": round(float(keyframe["timestamp"]), PRECISION),
        "ease_progress": round(float(keyframe["ease_progress"]), PRECISION),
        "scalar": round(float(keyframe["scalar"]), PRECISION),
        "transform_matrix": np.round(keyframe["transform_matrix"], PRECISION).tolist(),
        "positions": np.round(keyframe["positions"], PRECISION).tolist(),
    }


def _serialize_camera_keyframe(keyframe: dict) -> dict:
    return {
        "frame": int(keyframe["frame"]),
        "timestamp": round(float(keyframe["timestamp"]), PRECISION),
        "position": np.round(keyframe["position"], PRECISION).tolist(),
        "orientation_quaternion": np.round(
            keyframe["orientation_quaternion"], PRECISION).tolist(),
        "fov_deg": round(float(keyframe["fov_deg"]), PRECISION),
        "frustum_clear": bool(keyframe["frustum_clear"]),
    }


def load_model(payload: dict) -> Model:
    """从 HTTP/CLI 输入加载模型：支持 obj_text 或顶点/面片数组两种形式。"""
    if payload.get("obj_text"):
        return parse_obj(payload["obj_text"])
    if payload.get("vertices") and payload.get("faces"):
        return model_from_arrays(
            payload["vertices"], payload["faces"], payload.get("material_groups")
        )
    raise PipelineError("缺少模型数据：需提供 obj_text 或 vertices/faces")


def run_pipeline(request: dict) -> dict:
    """执行完整流水线，返回可直接 JSON 序列化的响应字典（确定性输出）。"""
    fps = int(request.get("fps", 25))
    if fps <= 0:
        raise PipelineError("fps 必须为正整数")
    thresholds = dict(DEFAULT_THRESHOLDS)
    thresholds.update(request.get("smoothness_thresholds") or {})

    model = load_model(request)
    try:
        case = CaseConfig(request["case_bounds"])
    except KeyError:
        raise PipelineError("缺少 case_bounds 展柜边界配置")
    validate_model_inside_case(model, case)
    cam = CameraConfig(request.get("camera") or {})

    script_entries = request.get("script")
    if not script_entries:
        raise PipelineError("缺少 script 展陈脚本条目")
    try:
        parsed_actions = parse_script(script_entries)
    except UnparsedEntry as exc:
        raise UnparsedAction(exc.index, exc.text, exc.reason)

    occupancy = boundary_occupancy(model, case)
    clips = []
    constraint_records = []
    summaries = []
    verifications = []

    for parsed in parsed_actions:
        record = solve_action_constraint(parsed, model, case)
        constraint_records.append(record)

        trajectory = build_trajectory(parsed, record, model, fps)
        camera_trajectory = build_camera_trajectory(
            model, case, cam, trajectory["timestamps"], parsed["index"]
        )

        model_metrics = evaluate(
            trajectory["keyframes"], trajectory["timestamps"], "model", thresholds
        )
        camera_metrics = evaluate(
            camera_trajectory["keyframes"], trajectory["timestamps"],
            "camera", thresholds,
        )

        resampling = {"resampled": False}
        if needs_resampling(model_metrics, camera_metrics):
            corrected = _apply_resampling(
                parsed, model, cam, case, trajectory, camera_trajectory, fps
            )
            trajectory, camera_trajectory = corrected["trajectories"]
            model_metrics = evaluate(
                trajectory["keyframes"], trajectory["timestamps"], "model", thresholds
            )
            camera_metrics = evaluate(
                camera_trajectory["keyframes"], trajectory["timestamps"],
                "camera", thresholds,
            )
            resampling = correction_summary(
                corrected["before_model"], corrected["before_camera"],
                model_metrics, camera_metrics,
            )

        verification = verify_action(
            parsed, record, trajectory["keyframes"], camera_trajectory
        )
        verifications.append(verification)

        clip = {
            "action_index": parsed["index"],
            "action_type": parsed["action_type"],
            "direction": parsed["direction"],
            "period": round(float(parsed["period"]), PRECISION),
            "fps": fps,
            "frame_count": len(trajectory["keyframes"]),
            "model_keyframes": [_serialize_keyframe(k) for k in trajectory["keyframes"]],
            "camera_keyframes": [
                _serialize_camera_keyframe(k) for k in camera_trajectory["keyframes"]
            ],
        }
        clips.append(clip)

        summaries.append({
            "index": parsed["index"],
            "raw_text": parsed["raw_text"],
            "direction": parsed["direction"],
            "easing": parsed["easing"],
            "amplitude_requested": record["amplitude_requested"],
            "amplitude_adjusted": record["amplitude_adjusted"],
            "period": round(float(parsed["period"]), PRECISION),
            "constraint_status": record["status"],
            "boundary_occupancy_ratio": round(float(occupancy), PRECISION),
            "model_smoothness": model_metrics,
            "camera_smoothness": camera_metrics,
            "resampling": resampling,
            "camera_all_frames_clear": camera_trajectory["all_frames_clear"],
        })

    return _round({
        "status": "OK",
        "precision": PRECISION,
        "fps": fps,
        "model": model.to_payload(PRECISION),
        "parsed_actions": parsed_actions,
        "constraint_records": constraint_records,
        "clips": clips,
        "summary_report": {
            "action_count": len(parsed_actions),
            "boundary_occupancy_ratio": round(float(occupancy), PRECISION),
            "actions": summaries,
        },
        "verification_report": {
            "thresholds": {
                "direction_deg": 15.0,
                "amplitude_relative": 0.05,
                "period_relative": 0.02,
            },
            "actions": verifications,
            "all_passed": all(item["passed"] for item in verifications),
        },
    })


def _apply_resampling(parsed, model, cam, case, trajectory, camera_trajectory, fps):
    """弧长时间重采样：修正标量轨迹与相机位置，总周期与帧数保持不变。"""
    timestamps = trajectory["timestamps"]
    sample_count = len(timestamps)
    before_model = evaluate(trajectory["keyframes"], timestamps, "model", DEFAULT_THRESHOLDS)
    before_camera = evaluate(camera_trajectory["keyframes"], timestamps, "camera",
                             DEFAULT_THRESHOLDS)

    corrected_scalars = resample_scalar_curve(trajectory["scalars"], sample_count)
    rebuilt_keyframes = rebuild_trajectory_from_scalars(
        parsed, model, timestamps, corrected_scalars
    )
    new_trajectory = dict(trajectory)
    new_trajectory["keyframes"] = rebuilt_keyframes
    new_trajectory["scalars"] = corrected_scalars

    camera_positions = camera_representative_points(camera_trajectory["keyframes"])
    corrected_positions = resample_camera_positions(camera_positions, sample_count)
    new_camera = build_camera_trajectory(
        model, case, cam, timestamps, parsed["index"]
    )
    for keyframe, position in zip(new_camera["keyframes"], corrected_positions):
        keyframe["position"] = position
    # 位置修正后重新计算朝向与视锥判定
    from .camera import _view_rotation, rotation_matrix_to_quaternion, frustum_intersects_aabb
    target = model.center + cam.look_at_offset
    world_up = np.array([0.0, 0.0, 1.0])
    clear_flags = []
    for keyframe in new_camera["keyframes"]:
        forward = target - keyframe["position"]
        rotation = _view_rotation(forward, world_up)
        keyframe["orientation_quaternion"] = rotation_matrix_to_quaternion(rotation)
        intersects = frustum_intersects_aabb(
            keyframe["position"], rotation, cam, model.aabb_min, model.aabb_max
        )
        keyframe["frustum_clear"] = not intersects
        clear_flags.append(not intersects)
    new_camera["all_frames_clear"] = bool(all(clear_flags))

    return {
        "trajectories": (new_trajectory, new_camera),
        "before_model": before_model,
        "before_camera": before_camera,
    }
