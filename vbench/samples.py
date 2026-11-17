"""运动时序样本（轨迹 JSONL、语义标签 JSONL、图生初始图 PNG）加载与校验。"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from PIL import Image

from .config import Config
from .errors import ErrorCode, Failure

PRIMITIVE_KEYS = ("position", "velocity", "acceleration")
DEFORMATION_KEYS = ("scale", "rotation")


@dataclass(frozen=True)
class MotionFrame:
    index: int
    position: Tuple[float, float]
    velocity: Tuple[float, float]
    acceleration: Tuple[float, float]
    scale: float
    rotation: float

    def vector(self) -> np.ndarray:
        return np.array([
            self.position[0], self.position[1],
            self.velocity[0], self.velocity[1],
            self.acceleration[0], self.acceleration[1],
            self.scale, self.rotation,
        ], dtype=np.float32)


@dataclass
class Clip:
    clip_id: str
    prompt: str
    frames: List[MotionFrame]
    labels: List[str]
    init_images: Dict[str, np.ndarray] = field(default_factory=dict)
    init_image_fingerprints: Dict[str, str] = field(default_factory=dict)


def _read_jsonl(path: str) -> List[Dict[str, Any]]:
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    records: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_no} JSON 解析失败: {exc}") from exc
    return records


def _validate_motion_frame(raw: Any, index: int) -> MotionFrame:
    if not isinstance(raw, dict):
        raise ValueError(f"第 {index} 帧不是对象")
    for key in PRIMITIVE_KEYS:
        value = raw.get(key)
        if not isinstance(value, list) or len(value) != 2 \
                or not all(isinstance(v, (int, float)) for v in value):
            raise ValueError(f"第 {index} 帧字段 {key} 必须是长度 2 的数值数组")
    deformation = raw.get("deformation")
    if not isinstance(deformation, dict) or any(
            key not in deformation or not isinstance(deformation[key], (int, float))
            for key in DEFORMATION_KEYS):
        raise ValueError(f"第 {index} 帧 deformation 必须含数值字段 {DEFORMATION_KEYS}")
    return MotionFrame(
        index=index,
        position=(float(raw["position"][0]), float(raw["position"][1])),
        velocity=(float(raw["velocity"][0]), float(raw["velocity"][1])),
        acceleration=(float(raw["acceleration"][0]), float(raw["acceleration"][1])),
        scale=float(deformation["scale"]),
        rotation=float(deformation["rotation"]),
    )


def load_clips(sample_dir: str, config: Config
               ) -> Tuple[List[Clip], List[Failure]]:
    """加载并校验样本。返回合法片段列表（保持文件顺序）与失败清单。"""
    failures: List[Failure] = []
    traj_path = os.path.join(sample_dir, "trajectories.jsonl")
    label_path = os.path.join(sample_dir, "labels.jsonl")
    try:
        traj_records = _read_jsonl(traj_path)
        label_records = _read_jsonl(label_path)
    except (FileNotFoundError, ValueError) as exc:
        failures.append(Failure(
            code=ErrorCode.E_TIMESERIES_INVALID,
            message=f"运动时序输入不可读: {exc}"))
        return [], failures

    def _index_records(records: List[Dict[str, Any]], kind: str
                       ) -> Tuple[Dict[str, Dict[str, Any]], List[Failure]]:
        indexed: Dict[str, Dict[str, Any]] = {}
        local_failures: List[Failure] = []
        for record in records:
            clip_id = record.get("clip_id")
            if not isinstance(clip_id, str) or not clip_id:
                local_failures.append(Failure(
                    code=ErrorCode.E_TIMESERIES_INVALID,
                    message=f"{kind}记录缺少合法 clip_id",
                    payload={"record": record}))
                continue
            if clip_id in indexed:
                local_failures.append(Failure(
                    code=ErrorCode.E_TIMESERIES_INVALID,
                    message=f"{kind}中片段 ID 重复: {clip_id}",
                    clip_id=clip_id))
                continue
            indexed[clip_id] = record
        return indexed, local_failures

    traj_index, fail_a = _index_records(traj_records, "轨迹")
    label_index, fail_b = _index_records(label_records, "标签")
    failures.extend(fail_a)
    failures.extend(fail_b)

    clips: List[Clip] = []
    for clip_id, record in traj_index.items():
        label_record = label_index.get(clip_id)
        if label_record is None:
            failures.append(Failure(
                code=ErrorCode.E_TIMESERIES_INVALID,
                message="标签序列缺少对应片段", clip_id=clip_id))
            continue
        raw_frames = record.get("frames")
        labels = label_record.get("labels")
        if not isinstance(raw_frames, list) or not raw_frames:
            failures.append(Failure(
                code=ErrorCode.E_TIMESERIES_INVALID,
                message="轨迹 frames 必须是非空数组", clip_id=clip_id))
            continue
        if not isinstance(labels, list) or not labels:
            failures.append(Failure(
                code=ErrorCode.E_TIMESERIES_INVALID,
                message="语义 labels 必须是非空数组", clip_id=clip_id))
            continue
        if len(raw_frames) != len(labels):
            failures.append(Failure(
                code=ErrorCode.E_TIMESERIES_INVALID,
                message=(f"轨迹帧数 {len(raw_frames)} 与标签帧数 "
                         f"{len(labels)} 不一致"),
                clip_id=clip_id,
                payload={"trajectory_frames": len(raw_frames),
                         "label_frames": len(labels)}))
            continue
        if len(raw_frames) != config.frames:
            failures.append(Failure(
                code=ErrorCode.E_TIMESERIES_INVALID,
                message=(f"片段帧数 {len(raw_frames)} 与配置帧数 "
                         f"{config.frames} 不一致"),
                clip_id=clip_id,
                payload={"clip_frames": len(raw_frames),
                         "config_frames": config.frames}))
            continue
        try:
            motion_frames = [
                _validate_motion_frame(raw, idx)
                for idx, raw in enumerate(raw_frames)
            ]
        except ValueError as exc:
            failures.append(Failure(
                code=ErrorCode.E_TIMESERIES_INVALID,
                message=f"逐帧几何图元校验失败: {exc}", clip_id=clip_id))
            continue

        prompt = record.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            failures.append(Failure(
                code=ErrorCode.E_TIMESERIES_INVALID,
                message="轨迹记录缺少合法 prompt 文本", clip_id=clip_id))
            continue

        clip = Clip(
            clip_id=clip_id, prompt=prompt, frames=motion_frames,
            labels=[str(token) for token in labels])
        image_failures = _load_init_images(sample_dir, config, clip)
        failures.extend(image_failures)
        if not image_failures:
            clips.append(clip)

    return clips, failures


def _load_init_images(sample_dir: str, config: Config, clip: Clip
                      ) -> List[Failure]:
    failures: List[Failure] = []
    for tier in config.tiers:
        rel = os.path.join("init_images", tier.name, f"{clip.clip_id}.png")
        path = os.path.join(sample_dir, rel)
        if not os.path.isfile(path):
            failures.append(Failure(
                code=ErrorCode.E_TIMESERIES_INVALID,
                message=f"缺少图生初始图: {rel}",
                clip_id=clip_id, tier=tier.name))
            continue
        try:
            with Image.open(path) as image:
                image.load()
                if image.size != (tier.width, tier.height):
                    failures.append(Failure(
                        code=ErrorCode.E_TIMESERIES_INVALID,
                        message=(f"初始图尺寸 {image.size} 与档位 "
                                 f"{(tier.width, tier.height)} 不符"),
                        clip_id=clip_id, tier=tier.name))
                    continue
                rgb = np.asarray(image.convert("RGB"), dtype=np.uint8)
        except Exception as exc:  # pragma: no cover - 损坏 PNG 用例
            failures.append(Failure(
                code=ErrorCode.E_TIMESERIES_INVALID,
                message=f"初始图无法读取: {exc}",
                clip_id=clip_id, tier=tier.name,
                exception=type(exc).__name__))
            continue
        clip.init_images[tier.name] = rgb
        clip.init_image_fingerprints[tier.name] = _array_fp(rgb)
    return failures


def _array_fp(array: np.ndarray) -> str:
    from .hashing import fingerprint_array
    return fingerprint_array(array.astype(np.float32))
