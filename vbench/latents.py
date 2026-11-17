"""潜变量初始化：文生提示词路径与图生图像编码路径。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Tuple

import numpy as np

from .config import Config, Tier
from .embedding import HashBagOfWords
from .hashing import DeterministicRNG, fingerprint_array, stable_hash


@dataclass(frozen=True)
class InitArtifact:
    path: str                      # "text" | "image"
    tier: str
    latent: np.ndarray             # (frames, C, h, w)
    fingerprint: str
    dependencies: Dict[str, Any]

    def to_manifest(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "tier": self.tier,
            "shape": list(self.latent.shape),
            "fingerprint": self.fingerprint,
            "dependencies": self.dependencies,
        }


def _image_to_latent(image: np.ndarray, tier: Tier, channels: int,
                     clip_id: str, seed: int) -> np.ndarray:
    """确定性“图像编码器”：8 倍下采样 + 逐通道确定性混合。

    采用固定 8x8 平均池化模拟编码器空间压缩，再以 clip/seed 派生的
    线性混合矩阵映射到 latent_channels，全程无随机副作用。
    """
    rgb = image.astype(np.float32) / 255.0
    height, width, _ = rgb.shape
    block_h, block_w = height // 8, width // 8
    pooled = np.zeros((block_h, block_w, 3), dtype=np.float32)
    for row in range(block_h):
        for col in range(block_w):
            block = rgb[row * 8:(row + 1) * 8, col * 8:(col + 1) * 8, :]
            pooled[row, col, :] = block.mean(axis=(0, 1))
    rng = DeterministicRNG("img-encode-mix", seed, clip_id, tier.name)
    mix = rng.uniform((3, channels), -0.5, 0.5)
    latent = pooled @ mix                                    # (h, w, C)
    latent = np.transpose(latent, (2, 0, 1))[None, ...]      # (1, C, h, w)
    return np.ascontiguousarray(latent, dtype=np.float32)


def initialize_text_latent(config: Config, clip_id: str, embed: np.ndarray,
                           tier: Tier) -> InitArtifact:
    rng = DeterministicRNG("init-text", config.seed, clip_id, tier.name)
    shape = (config.frames, config.latent_channels,
             tier.latent_shape[1], tier.latent_shape[2])
    noise = rng.normal(shape, scale=0.5)
    # 提示词嵌入作为每帧每通道的确定性偏置（广播到空间维）。
    channel_bias = embed[:config.latent_channels].reshape(1, -1, 1, 1)
    latent = noise + channel_bias
    fp = fingerprint_array(latent)
    return InitArtifact(
        path="text", tier=tier.name, latent=latent, fingerprint=fp,
        dependencies={
            "kind": "text",
            "seed": config.seed,
            "clip_id": clip_id,
            "tier": tier.name,
            "embed_fingerprint": fingerprint_array(embed),
            "frames": config.frames,
            "derivation": stable_hash("init-text", config.seed, clip_id, tier.name),
        })


def initialize_image_latent(config: Config, clip_id: str, image: np.ndarray,
                            image_fingerprint: str, tier: Tier) -> InitArtifact:
    base = _image_to_latent(image, tier, config.latent_channels,
                            clip_id, config.seed)
    rng = DeterministicRNG("init-image", config.seed, clip_id, tier.name)
    shape = (config.frames, config.latent_channels,
             tier.latent_shape[1], tier.latent_shape[2])
    jitter = rng.normal(shape, scale=0.1)
    latent = np.broadcast_to(base, shape).copy() + jitter
    fp = fingerprint_array(latent)
    return InitArtifact(
        path="image", tier=tier.name, latent=latent, fingerprint=fp,
        dependencies={
            "kind": "image",
            "seed": config.seed,
            "clip_id": clip_id,
            "tier": tier.name,
            "image_fingerprint": image_fingerprint,
            "frames": config.frames,
            "derivation": stable_hash("init-image", config.seed, clip_id, tier.name),
        })


def build_encoder(config: Config) -> HashBagOfWords:
    return HashBagOfWords(config.vocab, config.embed_dim)
