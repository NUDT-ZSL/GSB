"""数据集读取与输入校验。"""

import json
import os

import numpy as np

from .errors import (
    DomainError,
    E_DUP_CALL,
    E_FRAME_COUNT_MISMATCH,
    E_PROFILE_MISSING,
)
from .models import CallManifest, FrameObs


def load_manifest(manifest_path):
    manifests = []
    seen = set()
    with open(manifest_path, "r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            call_id = record.get("call_id")
            if call_id in seen:
                raise DomainError(
                    E_DUP_CALL,
                    f"重复 call_id: {call_id}",
                    call_id=call_id,
                    line_no=line_no,
                )
            seen.add(call_id)
            if not record.get("noise_profile_ref"):
                raise DomainError(
                    E_PROFILE_MISSING,
                    "noise_profile_ref 缺失",
                    call_id=call_id,
                    line_no=line_no,
                )
            manifests.append(
                CallManifest(
                    call_id=call_id,
                    seed=int(record["seed"]),
                    duration_ms=int(record["duration_ms"]),
                    frame_ms=int(record["frame_ms"]),
                    noise_profile_ref=record["noise_profile_ref"],
                )
            )
    return manifests


def load_observations(path):
    observations = []
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                observations.append(FrameObs(**json.loads(line)))
    return observations


def validate_call(manifest, observations):
    """跨文件一致性：duration_ms 与帧数、帧索引连续性、样本边界。"""
    expected_frames = manifest.duration_ms // manifest.frame_ms
    if len(observations) != expected_frames:
        raise DomainError(
            E_FRAME_COUNT_MISMATCH,
            "duration_ms 与帧级观测帧数不符",
            call_id=manifest.call_id,
            expected_frames=expected_frames,
            actual_frames=len(observations),
        )
    frame_len = 16000 * manifest.frame_ms // 1000
    for index, obs in enumerate(observations):
        if obs.frame_index != index:
            raise DomainError(
                E_FRAME_COUNT_MISMATCH,
                "帧索引不连续",
                call_id=manifest.call_id,
                frame_index=index,
            )
        if obs.sample_start != index * frame_len or obs.sample_end != (
            index + 1
        ) * frame_len:
            raise DomainError(
                E_FRAME_COUNT_MISMATCH,
                "帧样本边界不精确",
                call_id=manifest.call_id,
                frame_index=index,
                sample_start=obs.sample_start,
                sample_end=obs.sample_end,
            )


def load_pcm(path):
    return np.fromfile(path, dtype=np.int16)


def call_paths(dataset_dir, manifest):
    return (
        os.path.join(dataset_dir, f"{manifest.call_id}.pcm"),
        os.path.join(dataset_dir, f"{manifest.call_id}.frames.jsonl"),
    )

