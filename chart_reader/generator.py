"""Deterministic local sample generator.

Given a fixed seed and fixed canvas size, renders line / bar charts with
axes and tick marks using Pillow, and emits a ground-truth JSONL manifest
from the same drawing routine (image and truth are produced together).

Rendering uses only integer coordinates and Pillow's non-antialiased
primitives, so identical seeds produce byte-identical images.
"""

from __future__ import annotations

import hashlib
import json
import random
from pathlib import Path
from typing import Any, Dict, List, Tuple

from PIL import Image, ImageDraw

# Distinct, fully saturated series colors (never confused with black axes).
SERIES_PALETTE: List[Tuple[int, int, int]] = [
    (200, 30, 30),
    (30, 140, 40),
    (40, 60, 210),
    (190, 130, 0),
]

LAYOUT = {"left": 64, "right": 16, "top": 24, "bottom": 48}
N_TICKS = 6
TICK_LEN = 5
MARKER = 2  # marker half-size; marker block is (2*MARKER+1)=5 px wide


def _value_to_pixel_y(value: float, axis_min: float, axis_max: float,
                      y_base: int, y_top: int) -> int:
    frac = (value - axis_min) / (axis_max - axis_min)
    return int(round(y_base - frac * (y_base - y_top)))


def _draw_axes(draw: ImageDraw.ImageDraw, width: int, height: int,
               tick_values: List[float], axis_min: float, axis_max: float
               ) -> Tuple[List[int], int, int, int, int]:
    x0 = LAYOUT["left"]
    x1 = width - LAYOUT["right"] - 1
    y_top = LAYOUT["top"]
    y_base = height - LAYOUT["bottom"] - 1
    black = (0, 0, 0)
    draw.line([(x0, y_top), (x0, y_base)], fill=black, width=1)
    draw.line([(x0, y_base), (x1, y_base)], fill=black, width=1)
    tick_pixels: List[int] = []
    for tv in tick_values:
        ty = _value_to_pixel_y(tv, axis_min, axis_max, y_base, y_top)
        draw.line([(x0 - TICK_LEN, ty), (x0, ty)], fill=black, width=1)
        tick_pixels.append(ty)
    return tick_pixels, x0, x1, y_top, y_base


def generate_chart(chart_index: int, seed: int, width: int, height: int
                   ) -> Tuple[Image.Image, Dict[str, Any]]:
    """Render one chart and return (image, truth record)."""
    rng = random.Random(seed * 100003 + chart_index)
    chart_id = f"chart_{chart_index:04d}"
    chart_kind = "line" if chart_index % 2 == 0 else "bar"

    axis_min = 0.0
    axis_max = float(rng.choice([50, 100, 200, 250]))
    tick_values = [
        round(axis_min + (axis_max - axis_min) * i / (N_TICKS - 1), 6)
        for i in range(N_TICKS)
    ]

    img = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    tick_pixels, x0, x1, y_top, y_base = _draw_axes(
        draw, width, height, tick_values, axis_min, axis_max)

    n_series = 1 + (chart_index % 2)
    series_specs = []
    points: List[Dict[str, Any]] = []

    if chart_kind == "line":
        n_points = rng.randint(5, 8)
        span = (x1 - x0) - 40
        xs = [int(round(x0 + 20 + j * span / (n_points - 1))) for j in range(n_points)]
        # Draw polylines first, markers afterwards, so markers stay intact.
        per_series: List[List[Tuple[int, int, float]]] = []
        for s in range(n_series):
            pts = []
            for x in xs:
                value = round(rng.uniform(axis_min, axis_max), 4)
                py = _value_to_pixel_y(value, axis_min, axis_max, y_base, y_top)
                pts.append((x, py, value))
            per_series.append(pts)
        for s, pts in enumerate(per_series):
            color = SERIES_PALETTE[s % len(SERIES_PALETTE)]
            draw.line([(p[0], p[1]) for p in pts], fill=color, width=1)
        for s, pts in enumerate(per_series):
            color = SERIES_PALETTE[s % len(SERIES_PALETTE)]
            series_id = f"s{s}"
            values = []
            for (px, py, value) in pts:
                draw.rectangle([px - MARKER, py - MARKER, px + MARKER, py + MARKER],
                               fill=color)
                values.append(value)
                points.append({
                    "series_id": series_id,
                    "pixel_x": float(px),
                    "pixel_y": float(py),
                    "value": value,
                    "axis_min": axis_min,
                    "axis_max": axis_max,
                    "tick_pixels": list(tick_pixels),
                })
            series_specs.append({
                "series_id": series_id,
                "color": list(color),
                "values": values,
            })
    else:  # bar
        n_bars = rng.randint(6, 9)
        group_w = (x1 - x0) / n_bars
        bar_w = group_w * 0.8 / n_series
        for s in range(n_series):
            color = SERIES_PALETTE[s % len(SERIES_PALETTE)]
            series_id = f"s{s}"
            values = []
            for j in range(n_bars):
                value = round(rng.uniform(axis_min, axis_max), 4)
                py = _value_to_pixel_y(value, axis_min, axis_max, y_base, y_top)
                cx = x0 + group_w * (j + 0.5) + (s - (n_series - 1) / 2) * bar_w
                bx0 = int(round(cx - bar_w / 2))
                bx1 = int(round(cx + bar_w / 2))
                draw.rectangle([bx0, py, bx1, y_base - 1], fill=color)
                values.append(value)
                points.append({
                    "series_id": series_id,
                    "pixel_x": (bx0 + bx1) / 2.0,
                    "pixel_y": float(py),
                    "value": value,
                    "axis_min": axis_min,
                    "axis_max": axis_max,
                    "tick_pixels": list(tick_pixels),
                })
            series_specs.append({
                "series_id": series_id,
                "color": list(color),
                "values": values,
            })

    truth = {
        "chart_id": chart_id,
        "chart_kind": chart_kind,
        "canvas_shape": [height, width],
        "axis": {
            "axis_min": axis_min,
            "axis_max": axis_max,
            "tick_values": tick_values,
            "tick_pixels": [float(p) for p in tick_pixels],
            "axis_x": x0,
            "baseline_y": y_base,
        },
        "series": series_specs,
        "points": points,
    }
    return img, truth


def generate_samples(out_dir: str | Path, seed: int, count: int,
                     width: int = 640, height: int = 480) -> List[Dict[str, Any]]:
    """Generate `count` charts + truth.jsonl into `out_dir`. Returns manifests."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    records: List[Dict[str, Any]] = []
    truth_path = out / "truth.jsonl"
    with truth_path.open("w", encoding="utf-8", newline="\n") as fh:
        for i in range(count):
            img, truth = generate_chart(i, seed, width, height)
            img_path = out / f"{truth['chart_id']}.png"
            img.save(img_path, format="PNG")
            truth["image_file"] = img_path.name
            truth["image_sha256"] = hashlib.sha256(img_path.read_bytes()).hexdigest()
            fh.write(json.dumps(truth, ensure_ascii=False, sort_keys=True) + "\n")
            records.append(truth)
    return records


def load_truth(samples_dir: str | Path) -> List[Dict[str, Any]]:
    path = Path(samples_dir) / "truth.jsonl"
    records = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records
