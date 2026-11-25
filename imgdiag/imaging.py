from __future__ import annotations

import re
from typing import Any

import numpy as np

from .hashing import array_digest, sha256_text


TOKEN_RE = re.compile(r"[a-z0-9]+")


def generate_image(image_seed: int, image_shape: list[int] | tuple[int, ...]) -> np.ndarray:
    """Generate a deterministic RGB uint8 image.

    ``np.random.RandomState`` is used because its integer stream and legacy
    algorithms are stable across the supported NumPy versions.
    """
    shape = tuple(int(v) for v in image_shape)
    if len(shape) != 3 or shape[2] != 3 or min(shape[:2]) <= 0:
        raise ValueError("image_shape must be [height, width, 3]")
    height, width, _ = shape
    rng = np.random.RandomState(int(image_seed) & 0xFFFFFFFF)
    yy, xx = np.mgrid[0:height, 0:width].astype(np.float64)

    base = np.empty((height, width, 3), dtype=np.float64)
    base[..., 0] = 92.0 + 54.0 * np.sin(xx / max(2.0, width / 7.0)) + 18.0 * (yy / height)
    base[..., 1] = 118.0 + 42.0 * np.cos(yy / max(2.0, height / 6.0)) + 20.0 * (xx / width)
    base[..., 2] = 150.0 + 48.0 * np.sin((xx + yy) / max(3.0, width / 5.0))
    noise = rng.randint(-14, 15, size=(height, width, 3))
    broad = rng.rand(height // 4 + 2, width // 4 + 2, 3)
    # Nearest-neighbour upscale is deterministic and avoids image libraries.
    broad_up = np.repeat(np.repeat(broad, 4, axis=0), 4, axis=1)[:height, :width]
    image = base + noise.astype(np.float64) + (broad_up - 0.5) * 34.0
    return np.clip(np.rint(image), 0, 255).astype(np.uint8)


def image_hash(image: np.ndarray) -> str:
    return array_digest(np.ascontiguousarray(image))


def tokenize_prompt(prompt: str) -> list[str]:
    return TOKEN_RE.findall(str(prompt).lower())


def _token_vector(token: str, dim: int) -> np.ndarray:
    seed_hex = sha256_text(f"token:{token}")[:16]
    rng = np.random.RandomState(int(seed_hex, 16) & 0xFFFFFFFF)
    vector = rng.uniform(-1.0, 1.0, size=dim)
    return vector / max(np.linalg.norm(vector), 1e-12)


def text_condition_vector(prompt: str, vocab: list[str], dim: int = 24) -> dict[str, Any]:
    """Word-frequency weighted pooling over the controlled vocabulary."""
    tokens = tokenize_prompt(prompt)
    allowed = {word.lower() for word in vocab}
    unknown = sorted({token for token in tokens if token not in allowed})
    counts: dict[str, int] = {}
    for token in tokens:
        counts[token] = counts.get(token, 0) + 1
    total = max(1, sum(counts.values()))
    vector = np.zeros(int(dim), dtype=np.float64)
    weighted_terms = []
    for token in sorted(counts):
        frequency_weight = counts[token] / total
        embedding = _token_vector(token, dim)
        vector += frequency_weight * embedding
        weighted_terms.append({"token": token, "count": counts[token], "weight": frequency_weight})
    norm = float(np.linalg.norm(vector))
    if norm > 0:
        vector /= norm
    return {
        "vector": vector,
        "terms": weighted_terms,
        "unknown_terms": unknown,
        "dim": int(dim),
        "digest": array_digest(vector),
    }
