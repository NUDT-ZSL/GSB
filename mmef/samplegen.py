"""Deterministic local sample generation (fixed seed, no network)."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from PIL import Image

from .loader import CONFIG_DEFAULTS

SEED = 20261117


def _draw_sample(rng: np.random.RandomState, size: int) -> np.ndarray:
    img = rng.normal(128.0, 18.0, (size, size))
    for _ in range(int(rng.randint(2, 5))):
        intensity = float(rng.randint(40, 220))
        if rng.rand() < 0.5:
            x0, y0 = rng.randint(0, size // 2, 2)
            w = int(rng.randint(size // 8, size // 2))
            h = int(rng.randint(size // 8, size // 2))
            img[y0 : y0 + h, x0 : x0 + w] = intensity
        else:
            cx, cy = rng.randint(size // 4, 3 * size // 4, 2)
            rad = int(rng.randint(size // 10, size // 4))
            yy, xx = np.mgrid[0:size, 0:size]
            img[(yy - cy) ** 2 + (xx - cx) ** 2 <= rad * rad] = intensity
    return np.clip(img, 0, 255).astype(np.uint8)


def _strip_means(img: np.ndarray, dim: int) -> list:
    h = img.shape[0]
    return [
        float(img[i * h // dim : (i + 1) * h // dim].mean()) / 255.0
        for i in range(dim)
    ]


def _assertions_for(img: np.ndarray, dim: int, idx: int):
    means = _strip_means(img, dim)
    assertions = []
    for j, region in enumerate((idx % dim, (idx + 3) % dim, (idx + 5) % dim)):
        m = means[region]
        s = round(2.0 * m - 1.0, 3)  # signed brightness evidence in [-1, 1]
        if j == 1:
            s = -s  # a contradicting assertion for variety
        assertions.append(
            {
                "id": f"a{j + 1}",
                "entity": f"e{j + 1}",
                "claim": f"region_{region}_brightness_{'high' if s >= 0 else 'low'}",
                "class_logits": {
                    "supported": s,
                    "refuted": round(-s, 3),
                    "uncertain": round(0.1 * (1.0 - abs(s)), 3),
                },
            }
        )
    entities = [
        {"id": f"e{j + 1}", "kind": "region", "region": int((idx + 3 * j) % dim)}
        for j in range(3)
    ]
    attributes = [
        {"entity": f"e{j + 1}", "name": "mean_intensity",
         "value": round(means[(idx + 3 * j) % dim], 4)}
        for j in range(3)
    ]
    return {"entities": entities, "attributes": attributes, "assertions": assertions}


def generate(out_dir, num_samples: int = 8, seed: int = SEED) -> Path:
    out = Path(out_dir)
    samples_dir = out / "samples"
    samples_dir.mkdir(parents=True, exist_ok=True)
    size = CONFIG_DEFAULTS["image_size"]
    dim = CONFIG_DEFAULTS["feature_dim"]
    relations = []
    rel_types = ["associate", "supplement", "conflict"]
    for i in range(num_samples):
        name = f"sample_{i + 1:03d}"
        rng = np.random.RandomState(seed + i)
        img = _draw_sample(rng, size)
        sdir = samples_dir / name
        sdir.mkdir(parents=True, exist_ok=True)
        Image.fromarray(img, mode="L").save(sdir / "image.png")
        doc = _assertions_for(img, dim, i)
        (sdir / "assertions.json").write_text(
            json.dumps(doc, indent=2) + "\n", encoding="utf-8"
        )
        for j, a in enumerate(doc["assertions"]):
            relations.append(
                {
                    "sample": name,
                    "assertion_id": a["id"],
                    "region": int((i + 2 * j) % dim),
                    "type": rel_types[(i + j) % 3],
                    "weight": round(0.6 + 0.2 * ((i + j) % 3), 3),
                }
            )
    (out / "config.json").write_text(
        json.dumps(CONFIG_DEFAULTS, indent=2) + "\n", encoding="utf-8"
    )
    _make_edge_sample(samples_dir, relations, num_samples, size)
    (out / "relations.json").write_text(
        json.dumps({"relations": relations}, indent=2) + "\n", encoding="utf-8"
    )
    _make_invalid_samples(samples_dir, size)
    return out


def _make_edge_sample(samples_dir: Path, relations: list, num_samples: int,
                      size: int) -> None:
    """Crafted precision-sensitive sample: its top-two classes are close
    enough that the one-bit precision difference flips the final decision."""
    name = f"sample_{num_samples + 1:03d}"
    img = np.clip(
        np.random.RandomState(7).normal(128, 40, (size, size)), 0, 255
    ).astype(np.uint8)
    sdir = samples_dir / name
    sdir.mkdir(parents=True, exist_ok=True)
    Image.fromarray(img, mode="L").save(sdir / "image.png")
    doc = {
        "entities": [
            {"id": "e1", "kind": "region", "region": 1},
            {"id": "e2", "kind": "region", "region": 2},
        ],
        "attributes": [
            {"entity": "e1", "name": "mean_intensity", "value": 0.5},
            {"entity": "e2", "name": "mean_intensity", "value": 0.5},
        ],
        "assertions": [
            {
                "id": "a1",
                "entity": "e1",
                "claim": "region_1_nearly_neutral",
                "class_logits": {"supported": 0.065, "refuted": 0.0,
                                 "uncertain": -0.065},
            },
            {
                "id": "a2",
                "entity": "e2",
                "claim": "region_2_strongly_refuted",
                "class_logits": {"supported": 0.0, "refuted": 0.998,
                                 "uncertain": -0.998},
            },
        ],
    }
    (sdir / "assertions.json").write_text(
        json.dumps(doc, indent=2) + "\n", encoding="utf-8"
    )
    relations.append(
        {"sample": name, "assertion_id": "a1", "region": 1,
         "type": "associate", "weight": 1.0}
    )
    relations.append(
        {"sample": name, "assertion_id": "a2", "region": 2,
         "type": "associate", "weight": 1.0}
    )


def _make_invalid_samples(samples_dir: Path, size: int) -> None:
    valid_doc = {
        "entities": [{"id": "e1", "kind": "region", "region": 0}],
        "attributes": [],
        "assertions": [
            {
                "id": "a1",
                "entity": "e1",
                "claim": "x",
                "class_logits": {"supported": 0.1, "refuted": -0.1,
                                 "uncertain": 0.0},
            }
        ],
    }
    rng = np.random.RandomState(7)
    img = np.clip(rng.normal(128, 20, (size, size)), 0, 255).astype(np.uint8)

    d = samples_dir / "sample_bad_missing"
    d.mkdir(exist_ok=True)
    (d / "assertions.json").write_text(json.dumps(valid_doc), encoding="utf-8")

    d = samples_dir / "sample_bad_structure"
    d.mkdir(exist_ok=True)
    Image.fromarray(img, mode="L").save(d / "image.png")
    (d / "assertions.json").write_text(
        json.dumps({"entities": []}), encoding="utf-8"
    )

    d = samples_dir / "sample_bad_size"
    d.mkdir(exist_ok=True)
    Image.fromarray(img[: size // 2, : size // 2], mode="L").save(d / "image.png")
    (d / "assertions.json").write_text(json.dumps(valid_doc), encoding="utf-8")

    d = samples_dir / "sample_bad_header"
    d.mkdir(exist_ok=True)
    (d / "image.png").write_bytes(b"this is not a png file")
    (d / "assertions.json").write_text(json.dumps(valid_doc), encoding="utf-8")
