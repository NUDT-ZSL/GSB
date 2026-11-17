"""固定种子的本地确定性样本生成。

生成规则全部公开且仅依赖 numpy PCG64 + Pillow，重复运行逐字节一致。
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Sequence, Tuple

import numpy as np
from PIL import Image

from .hashing import DeterministicRNG

DEFAULT_VOCAB = [
    "circle", "square", "triangle", "moves", "rotates", "grows",
    "shrinks", "left", "right", "up", "down", "fast", "slow",
    "red", "blue", "green", "bounces", "drifts",
]
DEFAULT_TIERS = ["64x64", "128x128", "256x256"]


def _write_jsonl(path: str, records: List[Dict[str, Any]]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False,
                                    sort_keys=True) + "\n")


def _trajectory(clip_index: int, frames: int, seed: int
                ) -> List[Dict[str, Any]]:
    rng = DeterministicRNG("trajectory", seed, clip_index)
    position = rng.uniform((2,), -0.8, 0.8)
    velocity = rng.uniform((2,), -0.05, 0.05)
    acceleration = rng.uniform((2,), -0.002, 0.002)
    scale = float(rng.uniform((1,), 0.8, 1.2)[0])
    rotation = float(rng.uniform((1,), 0.0, 0.4)[0])
    output: List[Dict[str, Any]] = []
    for frame in range(frames):
        output.append({
            "index": frame,
            "position": [round(float(position[0]), 6),
                         round(float(position[1]), 6)],
            "velocity": [round(float(velocity[0]), 6),
                         round(float(velocity[1]), 6)],
            "acceleration": [round(float(acceleration[0]), 6),
                             round(float(acceleration[1]), 6)],
            "deformation": {
                "scale": round(float(scale + 0.05 * np.sin(frame * 0.3 + clip_index)), 6),
                "rotation": round(float(rotation + 0.02 * frame), 6),
            },
        })
        velocity = velocity + acceleration
        position = np.clip(position + velocity, -0.95, 0.95)
        if np.abs(position).max() >= 0.949:
            velocity = -velocity
    return output


def _labels(clip_index: int, frames: int, seed: int) -> List[str]:
    rng = DeterministicRNG("labels", seed, clip_index)
    vocabulary = ("enter", "move", "hold", "turn", "exit")
    return [vocabulary[rng.integers(0, len(vocabulary))]
            for _ in range(frames)]


def _init_image(clip_index: int, width: int, height: int,
                seed: int) -> np.ndarray:
    rng = DeterministicRNG("init-image", seed, clip_index, width, height)
    base = rng.uniform((height, width, 3), 0.0, 40.0)
    center_x = width // 2 + int(8 * np.sin(clip_index + 1))
    center_y = height // 2 + int(8 * np.cos(clip_index + 1))
    radius = max(4, width // 8)
    yy, xx = np.mgrid[0:height, 0:width]
    mask = (xx - center_x) ** 2 + (yy - center_y) ** 2 <= radius ** 2
    color = np.array([
        120 + 30 * (clip_index % 3),
        90 + 25 * ((clip_index + 1) % 3),
        150 - 20 * (clip_index % 4),
    ], dtype=np.float32)
    image = base.copy()
    image[mask] = np.clip(color + rng.uniform((3,), -8.0, 8.0), 0, 255)
    return image.astype(np.uint8)


def _table(kind: str, tier: str, steps: int, seed: int) -> np.ndarray:
    rng = DeterministicRNG(f"table-{kind}", seed, tier)
    if kind == "noise":
        values = np.linspace(0.9, 0.1, steps, dtype=np.float32)
        values = values + rng.uniform((steps,), -0.02, 0.02)
        return np.clip(values, 0.01, 0.95).astype(np.float32)
    values = np.linspace(0.2, 1.0, steps, dtype=np.float32)
    return np.clip(values + rng.uniform((steps,), -0.01, 0.01),
                   0.05, 1.5).astype(np.float32)


def generate_valid_sample(out_dir: str, *, clip_count: int = 4,
                          frames: int = 6, denoise_steps: int = 8,
                          seed: int = 20261117,
                          tiers: Sequence[str] = None,
                          embed_dim: int = 32,
                          replace_steps: int = 3,
                          concurrency: int = 4,
                          first_frame_budget_ms: float = 500.0,
                          denoise_step_budget: int | None = None,
                          memory_cap_mb: float = 2048.0,
                          vocab: Sequence[str] = None) -> str:
    tiers = list(tiers or DEFAULT_TIERS)
    vocab = list(vocab or DEFAULT_VOCAB)
    if denoise_step_budget is None:
        # 默认预算足以覆盖全部档位的“新增步”。
        from .denoise import checkpoint_for_tier
        total = 0
        for index in range(len(tiers)):
            checkpoint = checkpoint_for_tier(index, replace_steps, denoise_steps)
            new_steps = denoise_steps if index == 0 else denoise_steps - (checkpoint + 1)
            total += new_steps * frames * clip_count
        denoise_step_budget = total
    os.makedirs(out_dir, exist_ok=True)
    trajectories: List[Dict[str, Any]] = []
    labels: List[Dict[str, Any]] = []
    for index in range(clip_count):
        clip_id = f"clip_{index:03d}"
        shape_words = vocab[index % 3]
        motion_word = vocab[3 + (index % 4)]
        trajectories.append({
            "clip_id": clip_id,
            "prompt": f"{shape_words} {motion_word} {vocab[9 + index % 4]}",
            "frames": _trajectory(index, frames, seed),
        })
        labels.append({
            "clip_id": clip_id,
            "labels": _labels(index, frames, seed),
        })
    _write_jsonl(os.path.join(out_dir, "trajectories.jsonl"), trajectories)
    _write_jsonl(os.path.join(out_dir, "labels.jsonl"), labels)

    table_dir = os.path.join(out_dir, "tables")
    os.makedirs(table_dir, exist_ok=True)
    noise_paths: Dict[str, str] = {}
    guidance_paths: Dict[str, str] = {}
    for tier in tiers:
        width, height = (int(part) for part in tier.split("x"))
        np.save(os.path.join(table_dir, f"noise_{tier}.npy"),
                _table("noise", tier, denoise_steps, seed))
        np.save(os.path.join(table_dir, f"guidance_{tier}.npy"),
                _table("guidance", tier, denoise_steps, seed))
        noise_paths[tier] = f"tables/noise_{tier}.npy"
        guidance_paths[tier] = f"tables/guidance_{tier}.npy"
        for index in range(clip_count):
            image = _init_image(index, width, height, seed)
            image_dir = os.path.join(out_dir, "init_images", tier)
            os.makedirs(image_dir, exist_ok=True)
            Image.fromarray(image, mode="RGB").save(
                os.path.join(image_dir, f"clip_{index:03d}.png"),
                format="PNG")

    config = {
        "vocab": vocab,
        "embed_dim": embed_dim,
        "seed": seed,
        "denoise_steps": denoise_steps,
        "guidance_scale": 7.5,
        "frames": frames,
        "tiers": tiers,
        "img2img_replace_steps": replace_steps,
        "noise_tables": noise_paths,
        "guidance_tables": guidance_paths,
        "concurrency": concurrency,
        "first_frame_budget_ms": first_frame_budget_ms,
        "denoise_step_budget": denoise_step_budget,
        "memory_cap_mb": memory_cap_mb,
        "latent_channels": 4,
        "preview_cost_per_step_ms": 1.0,
        "fault_inject": {},
    }
    config_path = os.path.join(out_dir, "config.json")
    with open(config_path, "w", encoding="utf-8") as handle:
        json.dump(config, handle, ensure_ascii=False, indent=2, sort_keys=True)
    return config_path


def _copy_sample(src_dir: str, dst_dir: str) -> None:
    import shutil
    if os.path.isdir(dst_dir):
        shutil.rmtree(dst_dir)
    shutil.copytree(src_dir, dst_dir)


def _load_raw_config(sample_dir: str) -> Dict[str, Any]:
    with open(os.path.join(sample_dir, "config.json"), "r",
              encoding="utf-8") as handle:
        return json.load(handle)


def _dump_config(sample_dir: str, config: Dict[str, Any]) -> None:
    with open(os.path.join(sample_dir, "config.json"), "w",
              encoding="utf-8") as handle:
        json.dump(config, handle, ensure_ascii=False, indent=2,
                  sort_keys=True)


def generate_fault_suite(base_dir: str, valid_dir: str) -> Dict[str, str]:
    """在独立目录构造异常注入用例，不修改被测源码与合法样本。"""
    suite: Dict[str, str] = {}

    # 1) 配置缺字段（E_CONFIG_INVALID）
    dst = os.path.join(base_dir, "fault_missing_field")
    _copy_sample(valid_dir, dst)
    config = _load_raw_config(dst)
    config.pop("guidance_scale")
    _dump_config(dst, config)
    suite["missing_field"] = dst

    # 2) 噪声表长度不符（E_CONFIG_INVALID）
    dst = os.path.join(base_dir, "fault_bad_table_len")
    _copy_sample(valid_dir, dst)
    config = _load_raw_config(dst)
    tier = config["tiers"][0]
    wrong = np.linspace(0.9, 0.1, config["denoise_steps"] - 1,
                        dtype=np.float32)
    np.save(os.path.join(dst, f"tables/noise_{tier}.npy"), wrong)
    suite["bad_table_len"] = dst

    # 3) 去噪步预算为零（E_DENOISE_BUDGET_PENDING）
    dst = os.path.join(base_dir, "fault_zero_budget")
    _copy_sample(valid_dir, dst)
    config = _load_raw_config(dst)
    config["denoise_step_budget"] = 0
    _dump_config(dst, config)
    suite["zero_budget"] = dst

    # 4) 首帧预算为零（E_PREVIEW_BUDGET_PENDING）
    dst = os.path.join(base_dir, "fault_zero_preview")
    _copy_sample(valid_dir, dst)
    config = _load_raw_config(dst)
    config["first_frame_budget_ms"] = 0
    _dump_config(dst, config)
    suite["zero_preview"] = dst

    # 5) 帧数不一致（E_TIMESERIES_INVALID）
    dst = os.path.join(base_dir, "fault_frame_mismatch")
    _copy_sample(valid_dir, dst)
    path = os.path.join(dst, "labels.jsonl")
    with open(path, "r", encoding="utf-8") as handle:
        label_records = [json.loads(line) for line in handle if line.strip()]
    label_records[0]["labels"] = label_records[0]["labels"][:-1]
    _write_jsonl(path, label_records)
    suite["frame_mismatch"] = dst

    # 6) 片段 ID 重复（E_TIMESERIES_INVALID）
    dst = os.path.join(base_dir, "fault_duplicate_id")
    _copy_sample(valid_dir, dst)
    path = os.path.join(dst, "trajectories.jsonl")
    with open(path, "r", encoding="utf-8") as handle:
        records = [json.loads(line) for line in handle if line.strip()]
    if len(records) >= 2:
        records[1]["clip_id"] = records[0]["clip_id"]
    _write_jsonl(path, records)
    suite["duplicate_id"] = dst

    # 7) 非法引导强度（E_CONFIG_INVALID）
    dst = os.path.join(base_dir, "fault_bad_guidance")
    _copy_sample(valid_dir, dst)
    config = _load_raw_config(dst)
    config["guidance_scale"] = 999.0
    _dump_config(dst, config)
    suite["bad_guidance"] = dst

    # 8) 档位非单调（E_CONFIG_INVALID）
    dst = os.path.join(base_dir, "fault_non_monotonic")
    _copy_sample(valid_dir, dst)
    config = _load_raw_config(dst)
    config["tiers"] = list(reversed(config["tiers"]))
    _dump_config(dst, config)
    suite["non_monotonic"] = dst

    # 9) 共享段分歧注入（E_PATH_DIVERGENCE，业务层校验）
    dst = os.path.join(base_dir, "fault_path_divergence")
    _copy_sample(valid_dir, dst)
    config = _load_raw_config(dst)
    config["fault_inject"] = {"corrupt_shared_step": config["denoise_steps"] - 1}
    _dump_config(dst, config)
    suite["path_divergence"] = dst

    return suite
