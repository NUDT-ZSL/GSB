"""Deterministically generate all local samples and exchange projects.

Fixed-seed NumPy RNG produces RGB PNGs, rectangular damaged regions, paired v1
binary and v2 JSON projects, and four malformed projects for error-path tests.
No network access is required.
"""
from __future__ import annotations

import base64
import json
import struct
import sys
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ieditor.config import load_version_map  # noqa: E402
from ieditor.converter import upgrade_v1_to_v2, save_project  # noqa: E402
from ieditor.images import load_rgb  # noqa: E402
from ieditor.models import (MASK_DAMAGED, MASK_INTACT, PaletteEntry,  # noqa: E402
                            StyleParams, UnifiedProject)
from ieditor.parsers import MAGIC_V1, write_v1, write_v2  # noqa: E402

SEED = 20261117
WIDTH, HEIGHT = 48, 36
SAMPLES = ROOT / "samples"
CORRUPT = SAMPLES / "corrupt"


def _save_png(path: Path, array: np.ndarray) -> None:
    Image.fromarray(array.astype(np.uint8), mode="RGB").save(
        path, format="PNG", optimize=False)


def _damaged_image(rng: np.random.Generator) -> np.ndarray:
    yy, xx = np.mgrid[0:HEIGHT, 0:WIDTH]
    base = np.zeros((HEIGHT, WIDTH, 3), dtype=np.uint8)
    base[..., 0] = (xx * 5 + 20).clip(0, 255)
    base[..., 1] = (yy * 6 + 10).clip(0, 255)
    base[..., 2] = ((xx + yy) * 3 + 40).clip(0, 255)
    noise = rng.integers(0, 12, size=(HEIGHT, WIDTH, 3), dtype=np.int16)
    return np.clip(base.astype(np.int16) + noise, 0, 255).astype(np.uint8)


def _reference_image(rng: np.random.Generator) -> np.ndarray:
    yy, xx = np.mgrid[0:HEIGHT, 0:WIDTH]
    ref = np.zeros((HEIGHT, WIDTH, 3), dtype=np.uint8)
    ref[..., 0] = (180 + 40 * np.sin(xx / 7.0)).clip(0, 255)
    ref[..., 1] = (90 + 50 * np.cos(yy / 5.0)).clip(0, 255)
    ref[..., 2] = (60 + 30 * np.sin((xx + yy) / 9.0)).clip(0, 255)
    noise = rng.integers(0, 8, size=(HEIGHT, WIDTH, 3), dtype=np.int16)
    return np.clip(ref.astype(np.int16) + noise, 0, 255).astype(np.uint8)


def _rect_mask(rects) -> np.ndarray:
    mask = np.full((HEIGHT, WIDTH), MASK_INTACT, dtype=np.uint8)
    for x, y, w, h in rects:
        mask[y:y + h, x:x + w] = MASK_DAMAGED
    return mask


def build_projects() -> None:
    rng = np.random.default_rng(SEED)
    SAMPLES.mkdir(parents=True, exist_ok=True)
    CORRUPT.mkdir(parents=True, exist_ok=True)
    version_map = load_version_map()

    specs = [
        ("sample_a", [(10, 8, 14, 10)], 3),
        ("sample_b", [(6, 6, 10, 8), (28, 18, 12, 9)], 7),
        ("sample_c", [(18, 12, 16, 12)], 5),
    ]

    for project_id, rects, intensity in specs:
        image = _damaged_image(rng)
        reference = _reference_image(rng)
        image_path = SAMPLES / f"{project_id}_source.png"
        ref_path = SAMPLES / f"{project_id}_reference.png"
        _save_png(image_path, image)
        _save_png(ref_path, reference)

        mask = _rect_mask(rects)
        v1_project = UnifiedProject(
            project_id=project_id,
            source_version=1,
            image_path=image_path.name,
            style_ref_path=ref_path.name,
            mask=mask,
            style=StyleParams(intensity=intensity,
                              channel_weights={}, palette=[]),
            meta={"encoding": "binary_rle_1bit"},
        )
        v1_path = SAMPLES / f"{project_id}_v1.ied"
        write_v1(v1_project, v1_path)

        ref_rgb = load_rgb(ref_path)
        v2_project = upgrade_v1_to_v2(v1_project, version_map, ref_rgb)
        v2_path = SAMPLES / f"{project_id}_v2.json"
        write_v2(v2_project, v2_path)

    _build_corrupt_samples(specs)


def _build_corrupt_samples(specs) -> None:
    project_id, rects, intensity = specs[0]
    mask = _rect_mask(rects)
    image_name = f"{project_id}_source.png"
    ref_name = f"{project_id}_reference.png"

    # 1) illegal RLE: declared run count exceeds payload and zero-length run
    bad_rle = SAMPLES.parent / "samples" / "corrupt" / "bad_rle_v1.ied"
    header = struct.pack("<4sHIIiH", MAGIC_V1, 1, WIDTH, HEIGHT, intensity,
                         len(image_name))
    body = struct.pack(f"<{len(image_name)}sH", image_name.encode(),
                       len(ref_name))
    body += struct.pack(f"<{len(ref_name)}s", ref_name.encode())
    body += struct.pack("<I", 5)  # declares 5 runs
    body += bytes([0, 0])         # zero-length run -> illegal
    with open(bad_rle, "wb") as fh:
        fh.write(header + body)

    # 2) base64 length mismatch
    wrong_raw = bytes([0]) * (WIDTH * HEIGHT - 3)
    bad_b64 = CORRUPT / "bad_base64_v2.json"
    bad_b64_payload = {
        "magic": "IED2", "version": 2, "project_id": "bad_base64",
        "width": WIDTH, "height": HEIGHT,
        "image_path": image_name, "style_ref_path": ref_name,
        "mask_base64": base64.b64encode(wrong_raw).decode("ascii"),
        "palette_name": "bad",
        "style": {"intensity": intensity,
                  "channel_weights": {"R": 0.5, "G": 0.5, "B": 0.5},
                  "palette": []},
        "meta": {},
    }
    bad_b64.write_text(json.dumps(bad_b64_payload), encoding="utf-8")

    # 3) unknown version number (v3 magic inside a JSON envelope)
    bad_version = CORRUPT / "bad_version_v2.json"
    payload = dict(bad_b64_payload)
    payload["magic"] = "IED3"
    payload["version"] = 3
    payload["project_id"] = "bad_version"
    payload["mask_base64"] = base64.b64encode(
        mask.reshape(-1).tobytes()).decode("ascii")
    bad_version.write_text(json.dumps(payload), encoding="utf-8")

    # 4) missing channel weight (B absent)
    bad_weight = CORRUPT / "missing_weight_v2.json"
    payload = dict(bad_b64_payload)
    payload["project_id"] = "missing_weight"
    payload["mask_base64"] = base64.b64encode(
        mask.reshape(-1).tobytes()).decode("ascii")
    payload["style"] = {"intensity": intensity,
                        "channel_weights": {"R": 0.4, "G": 0.6},
                        "palette": [PaletteEntry("ink", [10, 20, 30]).to_dict()]}
    bad_weight.write_text(json.dumps(payload), encoding="utf-8")


def main() -> None:
    build_projects()
    print(f"samples generated under {SAMPLES} (seed={SEED})")
    for path in sorted(SAMPLES.rglob("*")):
        if path.is_file():
            print(" -", path.relative_to(ROOT))


if __name__ == "__main__":
    main()
