"""Local deterministic sample generation and render-contract checks."""

from __future__ import annotations

import dataclasses
import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Iterable

import numpy as np

from .util import sha256_json


TOKEN_PATTERN = re.compile(r"[a-z0-9]+|[\u3400-\u4dbf\u4e00-\u9fff]")

PROMPT_REFERENCES: tuple[tuple[str, str], ...] = (
    (
        "sunset over a calm sea with an orange sky and distant boat",
        "warm orange horizon, smooth water, small boat silhouette",
    ),
    (
        "snowy mountain village at blue hour with lit windows",
        "cool blue landscape, bright village windows, layered peaks",
    ),
    (
        "green forest path with sunlight and small red flowers",
        "dense green foliage, dappled light, red accents along a path",
    ),
    (
        "minimal geometric poster with navy circles and cream background",
        "clean composition, navy circular forms, cream negative space",
    ),
    (
        "夜色中的城市街道和暖色路灯",
        "深蓝天空，街道透视，温暖灯光，城市建筑",
    ),
    (
        "soft pastel abstract shapes with balanced negative space",
        "pastel palette, large simple shapes, balanced empty areas",
    ),
)


@dataclass(frozen=True)
class EvaluationSample:
    """A sample whose pixels are derived from ``seed`` and ``canvas``.

    ``image`` is optional in the wire representation. Evaluation regenerates it
    from the seed when needed. A caller may provide an image, but its shape and
    render signature must match the declared seed and canvas.
    """

    sample_id: str
    seed: int
    width: int
    height: int
    prompt: str
    reference: str
    truth_grade: int
    render_signature: str = ""
    image: np.ndarray | None = field(default=None, repr=False, compare=False)

    def to_wire(self) -> dict[str, Any]:
        return {
            "sample_id": self.sample_id,
            "seed": self.seed,
            "width": self.width,
            "height": self.height,
            "prompt": self.prompt,
            "reference": self.reference,
            "truth_grade": self.truth_grade,
            "render_signature": self.render_signature,
        }

    @classmethod
    def from_wire(cls, value: dict[str, Any], image: np.ndarray | None = None) -> "EvaluationSample":
        return cls(
            sample_id=value["sample_id"],
            seed=value["seed"],
            width=value["width"],
            height=value["height"],
            prompt=value["prompt"],
            reference=value["reference"],
            truth_grade=value["truth_grade"],
            render_signature=value["render_signature"],
            image=image,
        )


def tokenize(text: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", text).lower()
    return TOKEN_PATTERN.findall(normalized)


def render_image(seed: int, width: int, height: int) -> np.ndarray:
    """Render an RGB image deterministically from seed and canvas only."""
    if width <= 1 or height <= 1:
        raise ValueError("canvas dimensions must be greater than one")

    rng = np.random.default_rng(np.random.SeedSequence([int(seed), int(width), int(height)]))
    y_grid, x_grid = np.mgrid[0:height, 0:width]
    x_norm = x_grid.astype(np.float64) / float(width - 1)
    y_norm = y_grid.astype(np.float64) / float(height - 1)
    blend = np.clip(0.62 * y_norm + 0.38 * x_norm, 0.0, 1.0)

    color_a = rng.integers(20, 231, size=3).astype(np.float64)
    color_b = rng.integers(20, 231, size=3).astype(np.float64)
    canvas = color_a[None, None, :] * (1.0 - blend[..., None])
    canvas += color_b[None, None, :] * blend[..., None]

    phase_x = float(rng.uniform(0.0, 2.0 * np.pi))
    phase_y = float(rng.uniform(0.0, 2.0 * np.pi))
    wave = 10.0 * np.sin(7.0 * x_norm + phase_x) + 7.0 * np.cos(9.0 * y_norm + phase_y)
    canvas += wave[..., None] * np.array([0.55, 0.35, 0.10])[None, None, :]

    for _ in range(7):
        center_x = int(rng.integers(0, width))
        center_y = int(rng.integers(0, height))
        radius_x = int(rng.integers(max(3, width // 12), max(4, width // 4)))
        radius_y = int(rng.integers(max(3, height // 12), max(4, height // 4)))
        color = rng.integers(20, 240, size=3).astype(np.float64)
        alpha = float(rng.uniform(0.18, 0.48))
        x0 = max(0, center_x - radius_x)
        x1 = min(width, center_x + radius_x + 1)
        y0 = max(0, center_y - radius_y)
        y1 = min(height, center_y + radius_y + 1)
        if x0 >= x1 or y0 >= y1:
            continue
        yy, xx = np.mgrid[y0:y1, x0:x1]
        mask = (
            ((xx - center_x) / float(radius_x)) ** 2
            + ((yy - center_y) / float(radius_y)) ** 2
        ) <= 1.0
        region = canvas[y0:y1, x0:x1, :]
        region[mask] = region[mask] * (1.0 - alpha) + color[None, :] * alpha

    for _ in range(3):
        x0 = int(rng.integers(0, max(1, width - 1)))
        x1 = int(rng.integers(x0 + 1, width + 1))
        y0 = int(rng.integers(0, max(1, height - 1)))
        y1 = int(rng.integers(y0 + 1, height + 1))
        color = rng.integers(20, 245, size=3).astype(np.float64)
        alpha = float(rng.uniform(0.10, 0.32))
        region = canvas[y0:y1, x0:x1, :]
        region[:] = region * (1.0 - alpha) + color[None, None, :] * alpha

    noise = rng.normal(0.0, 2.2, size=(height, width, 3))
    canvas = np.clip(np.rint(canvas + noise), 0, 255).astype(np.uint8)
    return np.ascontiguousarray(canvas)


def render_signature(seed: int, width: int, height: int, image: np.ndarray) -> str:
    array = np.ascontiguousarray(image, dtype=np.uint8)
    return sha256_json(
        {
            "schema": "igeval.render/1",
            "seed": int(seed),
            "canvas": [int(width), int(height)],
            "pixels_sha256": hashlib.sha256(array.tobytes(order="C")).hexdigest(),
        }
    )


def make_sample(
    sample_id: str,
    seed: int,
    prompt: str,
    reference: str,
    truth_grade: int,
    *,
    width: int = 64,
    height: int = 64,
    image: np.ndarray | None = None,
) -> EvaluationSample:
    actual_image = render_image(seed, width, height) if image is None else np.array(
        image, dtype=np.uint8, copy=True, order="C"
    )
    signature = render_signature(seed, width, height, actual_image)
    return EvaluationSample(
        sample_id=sample_id,
        seed=seed,
        width=width,
        height=height,
        prompt=prompt,
        reference=reference,
        truth_grade=truth_grade,
        render_signature=signature,
        image=actual_image,
    )


def replace_sample(sample: EvaluationSample, **changes: Any) -> EvaluationSample:
    return dataclasses.replace(sample, **changes)


def build_demo_samples(
    count: int = 12,
    *,
    width: int = 64,
    height: int = 64,
) -> list[EvaluationSample]:
    """Build a controlled, reproducible mini-batch without external data."""
    if count <= 0:
        raise ValueError("count must be positive")
    samples: list[EvaluationSample] = []
    for index in range(count):
        prompt, reference = PROMPT_REFERENCES[index % len(PROMPT_REFERENCES)]
        seed = 4100 + index * 37
        truth_grade = 1 + ((index * 2 + 3) % 5)
        samples.append(
            make_sample(
                sample_id=f"sample-{index + 1:03d}",
                seed=seed,
                prompt=prompt,
                reference=reference,
                truth_grade=truth_grade,
                width=width,
                height=height,
            )
        )
    return samples


def validate_sample_shape(sample: EvaluationSample) -> None:
    if not isinstance(sample.sample_id, str) or not sample.sample_id.strip():
        raise ValueError("sample_id must be a non-empty string")
    for name in ("seed", "width", "height", "truth_grade"):
        value = getattr(sample, name)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{name} must be an integer")


def ensure_image(sample: EvaluationSample) -> np.ndarray:
    if sample.image is None:
        return render_image(sample.seed, sample.width, sample.height)
    return np.ascontiguousarray(sample.image, dtype=np.uint8)


def sorted_unique(values: Iterable[str]) -> list[str]:
    return sorted(set(values))

