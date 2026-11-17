#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"
LATENT_SHAPE = [8, 8, 4]
OUTPUT_SIZE = [32, 32]
FRAMES = 4
MAX_STEPS = 4


VOCABULARY = [
    "a",
    "across",
    "and",
    "at",
    "autumn",
    "bird",
    "blue",
    "bright",
    "calm",
    "city",
    "cloud",
    "copper",
    "coral",
    "dance",
    "dawn",
    "drift",
    "evening",
    "field",
    "flies",
    "forest",
    "fox",
    "gentle",
    "glow",
    "golden",
    "green",
    "lake",
    "light",
    "moon",
    "neon",
    "night",
    "orange",
    "over",
    "purple",
    "rain",
    "red",
    "robot",
    "runs",
    "silver",
    "sky",
    "snow",
    "snowy",
    "soft",
    "star",
    "sun",
    "through",
    "tree",
    "trees",
    "violet",
    "walks",
    "water",
    "winter",
]


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def generate_vocabulary() -> None:
    _write_json(SAMPLES / "vocab.json", {"version": 1, "words": VOCABULARY})


def generate_noise_tables() -> None:
    for steps in range(1, MAX_STEPS + 1):
        rows = []
        for step in range(steps):
            rng = np.random.default_rng(np.random.SeedSequence([20261117, step, 8, 8, 4]))
            row = rng.standard_normal(tuple(LATENT_SHAPE), dtype=np.float64) * 0.42
            rows.append(row)
        table = np.ascontiguousarray(np.stack(rows, axis=0), dtype=np.float64)
        np.save(SAMPLES / f"noise_table_{steps}.npy", table, allow_pickle=False)


def generate_init_image() -> None:
    width, height = OUTPUT_SIZE
    y, x = np.mgrid[0:height, 0:width]
    radial = np.sqrt(((x - width / 2) / (width / 2)) ** 2 + ((y - height / 2) / (height / 2)) ** 2)
    checker = ((x // 4) + (y // 4)) % 2
    red = np.clip(45 + 120 * (x / max(1, width - 1)) + 28 * checker - 38 * radial, 0, 255)
    green = np.clip(105 + 78 * (y / max(1, height - 1)) - 24 * checker + 16 * np.sin(x / 3.0), 0, 255)
    blue = np.clip(190 - 82 * radial + 32 * checker + 18 * np.cos(y / 2.5), 0, 255)
    pixels = np.stack([red, green, blue], axis=-1).astype(np.uint8)
    # Add a fixed local highlight so image-conditioned early steps are visibly distinct.
    pixels[6:13, 20:28, 0] = np.clip(pixels[6:13, 20:28, 0].astype(np.int16) + 55, 0, 255)
    pixels[6:13, 20:28, 1] = np.clip(pixels[6:13, 20:28, 1].astype(np.int16) + 40, 0, 255)
    Image.fromarray(pixels, mode="RGB").save(SAMPLES / "init_image.png", format="PNG", optimize=False)


def _base_config(*, output_dir: str, max_workers: int, prompt: str, seed: int) -> dict[str, object]:
    return {
        "config_version": 1,
        "mode": "t2v",
        "prompt": prompt,
        "seed": seed,
        "steps": 4,
        "guidance": 1.75,
        "frames": FRAMES,
        "latent_shape": LATENT_SHAPE,
        "output_size": OUTPUT_SIZE,
        "embed_dim": 48,
        "vocab_path": "samples/vocab.json",
        "noise_table_path": "samples/noise_table_4.npy",
        "max_workers": max_workers,
        "preview_size": [8, 8],
        "preview_budget_ms": 30,
        "output_dir": output_dir,
        "patch": None,
        "inject_failures": [],
    }


def _patch_values() -> list[list[list[float]]]:
    return [
        [
            [0.25, -0.15, 0.40, 0.05],
            [0.35, -0.05, 0.30, 0.15],
        ],
        [
            [0.45, 0.05, 0.20, 0.25],
            [0.55, 0.15, 0.10, 0.35],
        ],
    ]


def generate_configs() -> None:
    t2v = _base_config(
        output_dir="out/t2v",
        max_workers=4,
        prompt="a red fox runs through a snowy forest at dawn",
        seed=1729,
    )
    _write_json(SAMPLES / "t2v.json", t2v)
    _write_json(
        SAMPLES / "t2v_serial.json",
        {**t2v, "output_dir": "out/t2v_serial", "max_workers": 1},
    )
    i2v = {
        **_base_config(
            output_dir="out/i2v",
            max_workers=4,
            prompt="a blue bird flies over a calm lake at dawn",
            seed=4242,
        ),
        "mode": "i2v",
        "init_image_path": "samples/init_image.png",
        "i2v_replacement_steps": 2,
    }
    _write_json(SAMPLES / "i2v.json", i2v)
    _write_json(
        SAMPLES / "i2v_serial.json",
        {**i2v, "output_dir": "out/i2v_serial", "max_workers": 1},
    )


def generate_commands() -> None:
    patch_values = _patch_values()
    commands = {
        "version": 1,
        "base_config": "samples/t2v.json",
        "commands": [
            {
                "id": "prompt-change",
                "preview_budget_ms": 20,
                "set": {"prompt": "a blue bird flies over a calm lake at dawn"},
            },
            {"id": "seed-change", "preview_budget_ms": 20, "delay_before_ms": 800, "set": {"seed": 2026}},
            {"id": "guidance-change", "preview_budget_ms": 20, "set": {"guidance": 2.75}},
            {"id": "frame-count-change", "preview_budget_ms": 20, "set": {"frames": 3}},
            {
                "id": "step-count-change",
                "preview_budget_ms": 20,
                "set": {"steps": 3, "noise_table_path": "samples/noise_table_3.npy"},
            },
            {
                "id": "switch-to-i2v",
                "preview_budget_ms": 20,
                "set": {
                    "mode": "i2v",
                    "init_image_path": "samples/init_image.png",
                    "i2v_replacement_steps": 1,
                },
            },
            {
                "id": "apply-patch",
                "preview_budget_ms": 20,
                "set": {
                    "patch": {
                        "step": 1,
                        "frame": 1,
                        "x": 1,
                        "y": 2,
                        "values": patch_values,
                    }
                },
            },
            {"id": "remove-patch", "preview_budget_ms": 20, "set": {"patch": None}},
            {
                "id": "restore-step-count",
                "preview_budget_ms": 20,
                "set": {"steps": 4, "noise_table_path": "samples/noise_table_4.npy"},
            },
        ],
    }
    _write_json(SAMPLES / "interactive_commands.json", commands)


def generate_injection_cases() -> None:
    out_of_range_values = [[[0.0, 0.0, 0.0, 0.0], [0.0, 0.0, 0.0, 0.0]] for _ in range(2)]
    cases = {
        "version": 1,
        "base_config": "samples/t2v.json",
        "cases": [
            {
                "id": "seed-out-of-range",
                "set": {"seed": 4294967296},
                "expected_business_code": "E_CONFIG_INVALID",
            },
            {
                "id": "patch-out-of-range",
                "set": {
                    "patch": {
                        "step": 1,
                        "frame": 1,
                        "x": 7,
                        "y": 2,
                        "values": out_of_range_values,
                    }
                },
                "expected_business_code": "E_PATCH_OUT_OF_RANGE",
            },
            {
                "id": "noise-table-length-mismatch",
                "set": {"steps": 5},
                "expected_business_code": "E_NOISE_MISMATCH",
            },
            {
                "id": "type-error",
                "set": {"frames": "four"},
                "expected_business_code": "E_CONFIG_INVALID",
            },
            {
                "id": "missing-init-image",
                "set": {
                    "mode": "i2v",
                    "init_image_path": "samples/missing-init-image.png",
                    "i2v_replacement_steps": 1,
                },
                "expected_business_code": "E_INITIMAGE_INVALID",
            },
            {
                "id": "stage-failure-isolated",
                "set": {
                    "inject_failures": [
                        {"stage": "denoise", "step": 1, "frame": 2, "error_type": "RuntimeError"},
                        {"stage": "decode", "frame": 3, "error_type": "ValueError"},
                    ]
                },
                "expected_business_code": "E_STAGE_FAILED",
            },
        ],
    }
    _write_json(SAMPLES / "injection_cases.json", cases)


def generate_all() -> dict[str, object]:
    SAMPLES.mkdir(parents=True, exist_ok=True)
    generate_vocabulary()
    generate_noise_tables()
    generate_init_image()
    generate_configs()
    generate_commands()
    generate_injection_cases()
    if not (ROOT / "out").exists():
        (ROOT / "out").mkdir()
    return {
        "root": str(ROOT),
        "samples": sorted(path.name for path in SAMPLES.iterdir() if path.is_file()),
        "deterministic": True,
    }


if __name__ == "__main__":
    print(json.dumps(generate_all(), indent=2, sort_keys=True))
