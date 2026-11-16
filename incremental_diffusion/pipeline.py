"""Deterministic stage definitions and NumPy-only stage computations."""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from typing import Any, Mapping

import numpy as np

from .canonical import array_fingerprint, hash_object, normalized_array
from .config import LoadedConfig, Patch


PIPELINE_ALGORITHM_VERSION = "2026.11-v1"


@dataclass(frozen=True)
class StageSpec:
    stage_id: str
    kind: str
    dependencies: tuple[str, ...]
    params: dict[str, Any]
    parameter_fingerprint: str
    artifact_kind: str


def _parameter_fingerprint(algorithm: str, params: Mapping[str, Any]) -> str:
    return hash_object(
        {
            "pipeline_algorithm_version": PIPELINE_ALGORITHM_VERSION,
            "algorithm": algorithm,
            "params": dict(params),
        }
    )


def _patch_fingerprint(patches: list[Patch]) -> str:
    return hash_object({"patches": [patch.semantic_dict() for patch in patches]})


def build_stage_specs(config: LoadedConfig) -> list[StageSpec]:
    specs: list[StageSpec] = []

    prompt_params = {
        "prompt": config.prompt,
        "negative_prompt": config.negative_prompt,
        "vocab": list(config.vocab),
        "embedding_dim": config.embedding_dim,
    }
    specs.append(
        StageSpec(
            stage_id="prompt_encode",
            kind="prompt",
            dependencies=(),
            params=prompt_params,
            parameter_fingerprint=_parameter_fingerprint("hash_bag_prompt_v1", prompt_params),
            artifact_kind="embedding",
        )
    )

    latent_params = {
        "seed": config.seed,
        "width": config.width,
        "height": config.height,
        "latent_channels": config.latent_channels,
        "initial_scale": 0.85,
    }
    specs.append(
        StageSpec(
            stage_id="latent_init",
            kind="latent_init",
            dependencies=("prompt_encode",),
            params=latent_params,
            parameter_fingerprint=_parameter_fingerprint("latent_init_v1", latent_params),
            artifact_kind="latent",
        )
    )

    img2img_params = {
        "width": config.width,
        "height": config.height,
        "latent_channels": config.latent_channels,
        "initial_image_fingerprint": config.initial_image_fingerprint,
        "initial_image_present": config.initial_image is not None,
        "img2img_strength": config.img2img_strength,
        "skip_steps": config.skip_steps,
        "steps": config.steps,
    }
    specs.append(
        StageSpec(
            stage_id="img2img_encode",
            kind="img2img",
            dependencies=(),
            params=img2img_params,
            parameter_fingerprint=_parameter_fingerprint("img2img_encode_v1", img2img_params),
            artifact_kind="latent",
        )
    )

    previous_denoise: str | None = None
    for step in range(config.steps):
        dependencies: list[str] = ["prompt_encode"]
        if step == 0:
            dependencies.append("latent_init")
            if config.img2img_active:
                dependencies.append("img2img_encode")
        else:
            if config.img2img_active and step < config.skip_steps:
                dependencies.append("img2img_encode")
            if previous_denoise is not None:
                dependencies.append(previous_denoise)
        denoise_params = {
            "step": step,
            "steps": config.steps,
            "seed": config.seed,
            "guidance_scale": config.guidance_scale,
            "width": config.width,
            "height": config.height,
            "latent_channels": config.latent_channels,
            "noise_step_fingerprint": array_fingerprint(config.noise_table[step]),
            "patch_fingerprint": _patch_fingerprint(config.patches_by_step.get(step, [])),
            "img2img_active": config.img2img_active,
            "img2img_skip_steps": config.skip_steps,
        }
        stage_id = f"denoise_{step:02d}"
        specs.append(
            StageSpec(
                stage_id=stage_id,
                kind="denoise",
                dependencies=tuple(dependencies),
                params=denoise_params,
                parameter_fingerprint=_parameter_fingerprint("denoise_v1", denoise_params),
                artifact_kind="latent",
            )
        )
        previous_denoise = stage_id

    decode_params = {
        "seed": config.seed,
        "width": config.width,
        "height": config.height,
        "latent_channels": config.latent_channels,
        "clip_min": 0,
        "clip_max": 255,
    }
    specs.append(
        StageSpec(
            stage_id="decode",
            kind="decode",
            dependencies=(previous_denoise,) if previous_denoise else (),
            params=decode_params,
            parameter_fingerprint=_parameter_fingerprint("decode_linear_v1", decode_params),
            artifact_kind="image_rgb",
        )
    )
    return specs


def stage_order(specs: list[StageSpec]) -> list[str]:
    return [spec.stage_id for spec in specs]


def _rng_from_parts(*parts: Any) -> np.random.Generator:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(str(part).encode("utf-8"))
        digest.update(b"\x00")
    entropy = int.from_bytes(digest.digest(), "little")
    return np.random.Generator(np.random.PCG64(entropy))


def _prompt_vector(text: str, vocab: list[str], embedding_dim: int) -> np.ndarray:
    vocab_index = {token: index for index, token in enumerate(vocab)}
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    known = [token for token in tokens if token in vocab_index]
    features: list[tuple[str, float]] = [(token, 1.0) for token in known]
    for left, right in zip(known, known[1:]):
        features.append((f"{left}|{right}", 0.55))

    vector = np.zeros((embedding_dim,), dtype="<f8")
    for feature, weight in features:
        digest = hashlib.sha256(f"hash_bag_prompt_v1\x00{feature}".encode("utf-8")).digest()
        index = int.from_bytes(digest[:8], "little") % embedding_dim
        sign = 1.0 if (digest[8] & 1) else -1.0
        vector[index] += sign * weight
    norm = float(np.linalg.norm(vector))
    if norm > 0.0:
        vector /= norm
    return vector


def _prompt_encode(config: LoadedConfig) -> np.ndarray:
    positive = _prompt_vector(config.prompt, config.vocab, config.embedding_dim)
    negative = _prompt_vector(config.negative_prompt, config.vocab, config.embedding_dim)
    return np.stack([positive, negative], axis=0)


def _latent_init(config: LoadedConfig, upstream: Mapping[str, np.ndarray]) -> np.ndarray:
    prompt_data = normalized_array(upstream["prompt_encode"])
    digest = hashlib.sha256(prompt_data.tobytes(order="C")).digest()
    entropy = [config.seed]
    entropy.extend(int.from_bytes(digest[index : index + 4], "little") for index in range(0, 32, 4))
    seed_sequence = np.random.SeedSequence(entropy)
    rng = np.random.Generator(np.random.PCG64(seed_sequence))
    latent = rng.standard_normal(
        (config.latent_channels, config.height, config.width), dtype=np.float64
    )
    return np.ascontiguousarray(latent * 0.85, dtype="<f8")


def _img2img_encode(config: LoadedConfig) -> np.ndarray:
    if config.initial_image is None:
        return np.zeros(
            (config.latent_channels, config.height, config.width), dtype="<f8"
        )
    rng = _rng_from_parts(
        "img2img_encode_v1", config.height, config.width, config.latent_channels
    )
    weights = rng.normal(
        0.0, 1.0 / math.sqrt(3.0), (config.latent_channels, 3)
    )
    bias = rng.normal(0.0, 0.03, (config.latent_channels,))
    image = config.initial_image.astype("<f8") / 127.5 - 1.0
    projected = np.einsum("hwc,oc->ohw", image, weights, optimize=True)
    projected = projected + bias[:, None, None]
    return np.ascontiguousarray(np.tanh(projected) * 0.8, dtype="<f8")


def _condition_latent(config: LoadedConfig, prompt_embedding: np.ndarray) -> np.ndarray:
    rng = _rng_from_parts(
        "condition_projection_v1", config.seed, config.latent_channels, config.embedding_dim
    )
    weights = rng.normal(
        0.0, 1.0 / math.sqrt(config.embedding_dim),
        (config.latent_channels, config.embedding_dim),
    )
    positive, negative = prompt_embedding[0], prompt_embedding[1]
    delta = positive - negative
    return weights @ delta


def _apply_patches(latent: np.ndarray, patches: list[Patch]) -> np.ndarray:
    if not patches:
        return latent
    result = latent.copy()
    for patch in patches:
        result[
            :,
            patch.y : patch.y + patch.height,
            patch.x : patch.x + patch.width,
        ] = np.transpose(patch.values, (2, 0, 1))
    return result


def _denoise(
    config: LoadedConfig,
    step: int,
    upstream: Mapping[str, np.ndarray],
) -> np.ndarray:
    if step < config.skip_steps:
        latent = np.ascontiguousarray(upstream["img2img_encode"].copy(), dtype="<f8")
        return np.ascontiguousarray(
            _apply_patches(latent, config.patches_by_step.get(step, [])), dtype="<f8"
        )

    if step == 0:
        previous = upstream["latent_init"]
    else:
        previous = upstream[f"denoise_{step - 1:02d}"]

    guidance = _condition_latent(config, upstream["prompt_encode"]) * config.guidance_scale
    yy, xx = np.mgrid[0 : config.height, 0 : config.width]
    spatial = 0.75 + 0.25 * np.sin(
        0.29 * (xx.astype("<f8") + 1.0) + 0.19 * (yy.astype("<f8") + 1.0)
    )
    alpha = 0.84 + 0.04 * (1.0 - (step / max(config.steps, 1)))
    noise_scale = 0.30 * (1.0 - (step / (config.steps + 1.0)))
    latent = (
        alpha * previous
        + noise_scale * config.noise_table[step]
        + 0.075 * guidance[:, None, None] * spatial[None, :, :]
    )
    latent = _apply_patches(latent, config.patches_by_step.get(step, []))
    return np.ascontiguousarray(latent, dtype="<f8")


def _decode(config: LoadedConfig, upstream: Mapping[str, np.ndarray]) -> np.ndarray:
    rng = _rng_from_parts(
        "decode_projection_v1",
        config.seed,
        config.latent_channels,
        config.height,
        config.width,
    )
    weights = rng.normal(
        0.0, 18.0 / math.sqrt(config.latent_channels),
        (3, config.latent_channels),
    )
    bias = rng.normal(0.0, 4.0, (3,))
    latent = upstream[f"denoise_{config.steps - 1:02d}"]
    projected = np.einsum("chw,oc->ohw", latent, weights, optimize=True)
    projected = projected + bias[:, None, None]
    pixels = np.floor(np.clip(projected + 127.5, 0.0, 255.0) + 0.5)
    pixels = np.transpose(pixels, (1, 2, 0)).astype(np.uint8)
    return np.ascontiguousarray(pixels, dtype=np.uint8)


def compute_stage(
    spec: StageSpec,
    config: LoadedConfig,
    upstream: Mapping[str, np.ndarray],
) -> np.ndarray:
    if spec.kind == "prompt":
        return _prompt_encode(config)
    if spec.kind == "latent_init":
        return _latent_init(config, upstream)
    if spec.kind == "img2img":
        return _img2img_encode(config)
    if spec.kind == "denoise":
        return _denoise(config, int(spec.params["step"]), upstream)
    if spec.kind == "decode":
        return _decode(config, upstream)
    raise ValueError(f"unknown stage kind: {spec.kind}")
