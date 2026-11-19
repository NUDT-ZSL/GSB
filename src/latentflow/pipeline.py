from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import numpy as np

from .canonical import HashPRNG, array_sha256, prompt_condition
from .config import VideoConfig


@dataclass(frozen=True)
class GenerationResult:
    condition: np.ndarray
    initial_latent: np.ndarray
    latents: np.ndarray
    prompt_hash: str
    stage_hashes: dict[str, str]
    stage_timings_ms: dict[str, float]
    total_ms: float

    @property
    def latents_sha256(self) -> str:
        return array_sha256(self.latents)

    def public_metadata(self) -> dict[str, Any]:
        return {
            "prompt_hash": self.prompt_hash,
            "condition_sha256": array_sha256(self.condition),
            "initial_latent_sha256": array_sha256(self.initial_latent),
            "latents_sha256": self.latents_sha256,
            "stage_hashes": self.stage_hashes,
            "stage_timings_ms": self.stage_timings_ms,
            "total_ms": self.total_ms,
            "latent_shape": list(self.latents.shape),
            "latent_dtype": str(self.latents.dtype),
        }


def _spatial_condition(condition: np.ndarray, height: int, width: int) -> np.ndarray:
    phase = float(condition[4]) * 1.7 if condition.size > 4 else 0.0
    y = np.linspace(-1.0, 1.0, height, dtype=np.float32)[:, None]
    x = np.linspace(-1.0, 1.0, width, dtype=np.float32)[None, :]
    wave = np.sin((x * 2.3 + y * 1.7 + phase) * np.float32(np.pi))
    return wave.astype(np.float32)


def generate_latents(config: VideoConfig) -> GenerationResult:
    """Run all four deterministic stages and hash every latent checkpoint."""
    config.validate()
    t0 = time.perf_counter()

    condition_start = time.perf_counter()
    prompt_hash, condition = prompt_condition(config.prompt)
    condition_ms = (time.perf_counter() - condition_start) * 1000.0

    initial_start = time.perf_counter()
    initial_key = (
        f"initial-latent-v1|seed={config.seed}|shape={config.latent_shape}|"
        f"channels={config.latent_channels}"
    )
    latent_rng = HashPRNG(initial_key)
    initial = latent_rng.signed_floats(config.latent_shape).astype(np.float32, copy=False)
    initial_ms = (time.perf_counter() - initial_start) * 1000.0

    denoise_start = time.perf_counter()
    latent = initial.copy()
    stage_hashes: dict[str, str] = {
        "condition": array_sha256(condition),
        "initial_latent": array_sha256(initial),
    }
    frames, lh, lw, channels = config.latent_shape
    spatial = _spatial_condition(condition, lh, lw)
    cond_channels = condition[:channels].astype(np.float32)
    cond_field = cond_channels[None, None, None, :] * spatial[None, :, :, None]

    for step in range(config.steps):
        step_rng = HashPRNG(
            f"denoise-v1|seed={config.seed}|step={step}|shape={config.latent_shape}"
        )
        noise = step_rng.signed_floats(config.latent_shape).astype(np.float32, copy=False)
        progress = np.float32((step + 1) / config.steps)
        alpha = np.float32(0.86 - 0.18 * float(progress))
        target = (
            cond_field
            + np.float32(config.condition_strength) * noise
            + np.float32(0.08) * np.float32(1.0 - float(progress))
        )
        latent = (
            alpha * latent
            + np.float32(1.0 - float(alpha)) * target
            + np.float32(config.noise_strength * 0.05) * noise
        ).astype(np.float32, copy=False)
        stage_hashes[f"denoise_step_{step:02d}"] = array_sha256(latent)

    denoise_ms = (time.perf_counter() - denoise_start) * 1000.0
    stage_hashes["final_latent"] = array_sha256(latent)
    total_ms = (time.perf_counter() - t0) * 1000.0
    return GenerationResult(
        condition=condition,
        initial_latent=initial,
        latents=latent,
        prompt_hash=prompt_hash,
        stage_hashes=stage_hashes,
        stage_timings_ms={
            "prompt_hash": round(condition_ms, 6),
            "seed_initial_latent": round(initial_ms, 6),
            "denoise": round(denoise_ms, 6),
            "decode_estimate_reference": 0.0,
            "total_generation": round(total_ms, 6),
        },
        total_ms=total_ms,
    )
