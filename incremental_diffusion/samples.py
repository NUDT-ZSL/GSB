"""Generate tiny deterministic inputs for the local workbench."""

from __future__ import annotations

import copy
import io
import json
import math
import os
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from .canonical import atomic_write_bytes, atomic_write_json


DEFAULT_VOCAB = [
    "sunset",
    "ocean",
    "clouds",
    "mountain",
    "forest",
    "river",
    "city",
    "night",
    "blur",
    "noise",
    "warm",
    "blue",
    "red",
    "soft",
    "bright",
    "minimal",
]


def _save_png(path: Path, pixels: np.ndarray) -> None:
    buffer = io.BytesIO()
    Image.fromarray(pixels, mode="RGB").save(buffer, format="PNG", optimize=False)
    atomic_write_bytes(path, buffer.getvalue())


def _initial_image(width: int, height: int, *, alternate: bool = False) -> np.ndarray:
    yy, xx = np.mgrid[0:height, 0:width]
    x = xx.astype("<f8") / max(width - 1, 1)
    y = yy.astype("<f8") / max(height - 1, 1)
    if alternate:
        red = 35 + 120 * y + 35 * np.sin(5.0 * x)
        green = 55 + 130 * (1.0 - x) + 20 * np.cos(4.0 * y)
        blue = 170 - 80 * x + 35 * np.sin(3.0 * (x + y))
    else:
        red = 35 + 185 * x + 25 * np.sin(4.0 * y)
        green = 45 + 135 * y + 20 * np.cos(5.0 * x)
        blue = 105 + 70 * np.sin(3.0 * (x + y)) + 35 * (1.0 - y)
    return np.stack([red, green, blue], axis=2).clip(0, 255).astype(np.uint8)


def _patch_values(height: int, width: int, channels: int) -> list[Any]:
    yy, xx, cc = np.mgrid[0:height, 0:width, 0:channels]
    values = (
        0.32 * np.sin(0.9 * (xx + 1) + 0.4 * (yy + 1))
        + 0.11 * np.cos(0.7 * (cc + 1))
        + 0.04 * (cc - channels / 2)
    )
    return values.astype(float).round(8).tolist()


def _relative(path: Path, start: Path) -> str:
    return os.path.relpath(path, start)


def build_sample_inputs(
    input_dir: Path | str,
    *,
    workspace: Path | str | None = None,
    outputs: Path | str | None = None,
    width: int = 32,
    height: int = 32,
) -> dict[str, Path]:
    input_dir = Path(input_dir).resolve()
    input_dir.mkdir(parents=True, exist_ok=True)
    workspace_dir = (
        Path(workspace).resolve() if workspace is not None else input_dir.parent / "workspace"
    ).resolve()
    outputs_dir = (
        Path(outputs).resolve() if outputs is not None else input_dir.parent / "outputs"
    ).resolve()
    outputs_dir.mkdir(parents=True, exist_ok=True)

    initial_path = input_dir / "initial.png"
    alternate_path = input_dir / "initial_alt.png"
    _save_png(initial_path, _initial_image(width, height, alternate=False))
    _save_png(alternate_path, _initial_image(width, height, alternate=True))

    steps = 4
    latent_channels = 4
    seed_sequence = np.random.SeedSequence([99173, steps, latent_channels, height, width])
    rng = np.random.Generator(np.random.PCG64(seed_sequence))
    noise = rng.standard_normal(
        (steps, latent_channels, height, width), dtype=np.float32
    ).astype("<f4")
    noise_path = input_dir / "noise_table.npy"
    noise_buffer = io.BytesIO()
    np.save(noise_buffer, noise, allow_pickle=False)
    atomic_write_bytes(noise_path, noise_buffer.getvalue())
    atomic_write_json(
        input_dir / "noise_table_meta.json",
        {
            "shape": list(noise.shape),
            "dtype": noise.dtype.str,
            "seed_sequence": [99173, steps, latent_channels, height, width],
            "description": "Deterministic NumPy PCG64 noise table for the sample pipeline.",
        },
    )

    common: dict[str, Any] = {
        "schema_version": 1,
        "snapshot_version": 1,
        "seed": 2026,
        "prompt": "sunset ocean clouds warm",
        "negative_prompt": "blur noise",
        "steps": steps,
        "guidance_scale": 3.0,
        "width": width,
        "height": height,
        "latent_channels": latent_channels,
        "embedding_dim": 24,
        "vocab": list(DEFAULT_VOCAB),
        "noise_table": _relative(noise_path, input_dir),
        "initial_image": _relative(initial_path, input_dir),
        "img2img_strength": 0.5,
        "patches": [],
        "workspace": _relative(workspace_dir, input_dir),
        "output_image": _relative(outputs_dir / "final.png", input_dir),
        "report_json": _relative(outputs_dir / "diagnostic.json", input_dir),
        "graph_png": _relative(outputs_dir / "dependency_graph.png", input_dir),
        "intermediates_dir": _relative(outputs_dir / "intermediates", input_dir),
    }

    configs: dict[str, dict[str, Any]] = {}
    sample = copy.deepcopy(common)
    sample["snapshot_version"] = 1
    configs["sample_config.json"] = sample

    edit_prompt = copy.deepcopy(sample)
    edit_prompt["snapshot_version"] = 2
    edit_prompt["prompt"] = "mountain forest river soft"
    configs["edit_prompt.json"] = edit_prompt

    edit_seed = copy.deepcopy(edit_prompt)
    edit_seed["snapshot_version"] = 3
    edit_seed["seed"] = 777
    configs["edit_seed.json"] = edit_seed

    edit_guidance = copy.deepcopy(edit_seed)
    edit_guidance["snapshot_version"] = 4
    edit_guidance["guidance_scale"] = 5.5
    configs["edit_guidance.json"] = edit_guidance

    edit_patch = copy.deepcopy(edit_guidance)
    edit_patch["snapshot_version"] = 5
    edit_patch["patches"] = [
        {
            "step": 2,
            "x": 5,
            "y": 6,
            "width": 5,
            "height": 4,
            "values": _patch_values(4, 5, latent_channels),
        }
    ]
    configs["edit_patch.json"] = edit_patch

    edit_image = copy.deepcopy(edit_patch)
    edit_image["snapshot_version"] = 6
    edit_image["initial_image"] = _relative(alternate_path, input_dir)
    configs["edit_image.json"] = edit_image

    written: dict[str, Path] = {
        "initial_image": initial_path,
        "initial_image_alt": alternate_path,
        "noise_table": noise_path,
        "workspace": workspace_dir,
        "outputs": outputs_dir,
    }
    for filename, config in configs.items():
        path = input_dir / filename
        atomic_write_json(path, config)
        written[filename] = path
    return written


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Generate deterministic sample inputs")
    parser.add_argument("--input-dir", default="inputs")
    parser.add_argument("--workspace", default=None)
    parser.add_argument("--outputs", default=None)
    args = parser.parse_args()
    written = build_sample_inputs(
        args.input_dir, workspace=args.workspace, outputs=args.outputs
    )
    print(json.dumps({key: str(value) for key, value in written.items()}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
