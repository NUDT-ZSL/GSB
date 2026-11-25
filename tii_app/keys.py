"""Content-addressed stage keys, reconstructed independently from inputs."""

from __future__ import annotations

from typing import Any

from .models import Task
from .util import sha256_obj


KEY_CONTRACT = "tii-stage-key-v1"
FORWARD_CONTRACT = "tanh-linear-colchunk-v1"


def config_key(config: Any) -> str:
    return sha256_obj(
        {
            "contract": KEY_CONTRACT,
            "stage": "config",
            "generation": config.generation,
            "config_hash": config.config_hash,
        }
    )


def sample_key(task: Task, global_seed: int) -> str:
    return sha256_obj(
        {
            "contract": KEY_CONTRACT,
            "stage": "sample",
            "task_content_hash": task.content_hash,
            "global_seed": global_seed,
        }
    )


def quant_key(task: Task, sample_cache_key: str) -> str:
    return sha256_obj(
        {
            "contract": KEY_CONTRACT,
            "stage": "quantize",
            "sample_key": sample_cache_key,
            "dtype": task.dtype,
            "quant_scale": task.quant_scale,
            "quant_zero_point": task.quant_zero_point,
            "rounding": "half_away_from_zero",
        }
    )


def model_key(task: Task, config: Any) -> str:
    return sha256_obj(
        {
            "contract": KEY_CONTRACT,
            "stage": "model",
            "config_key": config_key(config),
            "feature_dim": task.feature_dim,
            "label_dim": task.label_dim,
            "weight_scale": config.hyperparameters.get("weight_scale"),
            "seed_offset": config.hyperparameters.get("seed_offset"),
        }
    )


def forward_key(task: Task, quant_cache_key: str, weight_key: str) -> str:
    return sha256_obj(
        {
            "contract": FORWARD_CONTRACT,
            "stage": "forward",
            "quant_key": quant_cache_key,
            "model_key": weight_key,
            "row_block": task.row_block,
            "col_block": task.col_block,
            "reduction_order": "row-then-col-chunks",
            "activation": "tanh",
        }
    )


def train_backward_key(task: Task, forward_cache_key: str) -> str:
    return sha256_obj(
        {
            "contract": KEY_CONTRACT,
            "stage": "train_backward",
            "forward_key": forward_cache_key,
            "loss": "mse_mean_v1",
            "mode": "train",
            "reduction_order": "row-then-col-chunks",
        }
    )


def inference_key(task: Task, forward_cache_key: str) -> str:
    return sha256_obj(
        {
            "contract": KEY_CONTRACT,
            "stage": "inference",
            "forward_key": forward_cache_key,
            "mode": "infer",
            "prediction": "argmax_tanh_logits",
        }
    )


def stage_keys(task: Task, config: Any, global_seed: int) -> dict[str, str]:
    keys = {"config": config_key(config)}
    keys["sample"] = sample_key(task, global_seed)
    keys["quantize"] = quant_key(task, keys["sample"])
    keys["model"] = model_key(task, config)
    keys["forward"] = forward_key(task, keys["quantize"], keys["model"])
    keys["train_backward"] = train_backward_key(task, keys["forward"])
    keys["inference"] = inference_key(task, keys["forward"])
    return keys
