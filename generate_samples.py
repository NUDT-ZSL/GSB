"""生成本地样例：60 帧序列、候选清单、两套阈值配置。

帧号 0-60 缺 25（共 60 个文件），用于演示“对应帧文件缺失”。
第 50 帧原始尺寸与基准不同，用于演示尺寸统一。
第 20、40 帧为同一静止画面（仅轻微噪声），用于演示近似重复合并。

用法：python generate_samples.py
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent
SAMPLES = ROOT / "samples"
FRAMES = SAMPLES / "frames"

WIDTH, HEIGHT = 640, 360
FPS = 25
FRAME_COUNT = 60  # 实际写出 60 个文件（0-60 中跳过 25）
MISSING_INDEX = 25
SMALL_INDEX = 50
SMALL_SIZE = (320, 180)
STATIC_INDICES = {20, 40}


def _grid(width: int, height: int) -> tuple[np.ndarray, np.ndarray]:
    x = np.linspace(0.0, 1.0, width, dtype=np.float32)
    y = np.linspace(0.0, 1.0, height, dtype=np.float32)
    return np.meshgrid(x, y)


def _to_image(channel: np.ndarray) -> Image.Image:
    base = np.clip(channel, 0, 255).astype(np.uint8)
    red = base
    green = np.clip(base * 0.82 + 26, 0, 255).astype(np.uint8)
    blue = np.clip(190 - base * 0.45, 0, 255).astype(np.uint8)
    rgb = np.stack([red, green, blue], axis=-1)
    return Image.fromarray(rgb, mode="RGB")


def _static_scene(width: int, height: int, seed: int) -> Image.Image:
    """静止的柔和渐变画面，仅有极小噪声；两个时间点几乎相同。"""

    x, y = _grid(width, height)
    rng = np.random.default_rng(seed)
    base = 70.0 + 55.0 * (0.6 * x + 0.4 * y)
    base += rng.uniform(-3.0, 3.0, size=(height, width)).astype(np.float32)
    return _to_image(base)


def _moving_scene(index: int, width: int, height: int) -> Image.Image:
    """不断移动的条纹与圆形，画面差异明显。"""

    x, y = _grid(width, height)
    phase = index * 0.14
    stripes = ((x * 6.0 + y * 3.0 + phase) % 1.0 < 0.5).astype(np.float32)
    channel = 38.0 + 120.0 * stripes

    cx = 0.12 + 0.76 * ((index * 0.071) % 1.0)
    cy = 0.5 + 0.22 * math.sin(index * 0.45)
    radius = 0.13
    inside = (x - cx) ** 2 + (y - cy) ** 2 < radius**2
    channel[inside] = 245.0 - 70.0 * ((x[inside] + y[inside]) % 0.25) * 4.0

    # 固定位置的高对比标记块，保证帧间差异不会退化为整体平移。
    if index % 7 == 0:
        block = (np.abs(x - 0.72) < 0.09) & (np.abs(y - 0.28) < 0.16)
        channel[block] = 15.0
    return _to_image(channel)


def _hero_scene(width: int, height: int) -> Image.Image:
    """用于第 50 帧的高差异画面（同时也是尺寸不一致的帧）。"""

    x, y = _grid(width, height)
    checks = ((np.floor(x * 9) + np.floor(y * 6)) % 2).astype(np.float32)
    channel = 20.0 + 200.0 * checks
    band = (y > 0.55) & (y < 0.78)
    channel[band] = 250.0 * np.clip((x[band] - 0.05) / 0.9, 0, 1)
    return _to_image(channel)


def build_frames() -> None:
    FRAMES.mkdir(parents=True, exist_ok=True)
    for old in FRAMES.glob("frame_*.png"):
        old.unlink()

    written = 0
    for index in range(FRAME_COUNT + 1):
        if index == MISSING_INDEX:
            continue
        if index == SMALL_INDEX:
            image = _hero_scene(*SMALL_SIZE)
        elif index in STATIC_INDICES:
            image = _static_scene(WIDTH, HEIGHT, seed=index)
        else:
            image = _moving_scene(index, WIDTH, HEIGHT)
        image.save(FRAMES / f"frame_{index:04d}.png", format="PNG")
        written += 1
    print(f"已生成 {written} 个帧文件：{FRAMES}")


def build_manifests() -> None:
    manifest = {
        "source": "宣传片 A 版 60 帧样例",
        "items": [
            {"label": "opening", "time": 0.2, "note": "开场"},
            {"label": "product", "time": 0.4, "note": "产品特写"},
            {"label": "static_a", "time": 0.8, "note": "静止画面（一）"},
            {"label": "static_b", "time": 1.6, "note": "静止画面（二），与上一项近似重复"},
            {"label": "hero", "time": 2.0, "note": "主视觉，原始尺寸不同于基准"},
        ],
    }
    (SAMPLES / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    edge_manifest = {
        "source": "越界、缺失与非法条目样例",
        "items": [
            {"label": "opening", "time": 0.2},
            {"label": "missing_frame", "time": 1.0, "note": "目标帧号 25，该文件不存在"},
            {"label": "out_of_range", "time": 99.0, "note": "远超序列末尾"},
            {"label": "static_a", "time": 0.8},
            {"label": "static_b", "time": 1.6},
            {"label": "no_time", "note": "缺少 time 字段"},
            {"label": "bad_time", "time": "abc", "note": "时间不是数值"},
        ],
    }
    (SAMPLES / "manifest_edge.json").write_text(
        json.dumps(edge_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"已生成清单：{SAMPLES / 'manifest.json'}")
    print(f"已生成清单：{SAMPLES / 'manifest_edge.json'}")


def build_configs() -> None:
    base = {
        "fps": FPS,
        "base_width": WIDTH,
        "base_height": HEIGHT,
        "tolerance_frames": 0.5,
        "frame_start": 0,
        "resize_mode": "fit",
    }
    (SAMPLES / "config.json").write_text(
        json.dumps({**base, "dedupe_threshold": 0.02}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (SAMPLES / "config_strict.json").write_text(
        json.dumps({**base, "dedupe_threshold": 0.002}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"已生成配置：{SAMPLES / 'config.json'}、{SAMPLES / 'config_strict.json'}")


def main() -> None:
    build_frames()
    build_manifests()
    build_configs()


if __name__ == "__main__":
    main()
