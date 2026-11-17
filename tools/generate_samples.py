#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path


def _rounded_glyph(index: int) -> dict:
    x_left = 42.0 + float((index * 17) % 23)
    x_right = 592.0 - float((index * 13) % 31)
    y_bottom = 20.0
    y_top = 690.0 + float((index * 19) % 61)
    radius = 72.0 + float((index * 7) % 19)

    points = [
        [x_left + radius, y_bottom],
        [x_right - radius, y_bottom],
        [x_right, y_bottom],
        [x_right, y_bottom + radius],
        [x_right, y_top - radius],
        [x_right, y_top],
        [x_right - radius, y_top],
        [x_left + radius, y_top],
        [x_left, y_top],
        [x_left, y_top - radius],
        [x_left, y_bottom + radius],
        [x_left, y_bottom],
    ]

    center_x = (x_left + x_right) * 0.5 + float((index % 2) * 2 - 1) * 18.0
    center_y = (y_bottom + y_top) * 0.5 + float((index % 3) - 1) * 21.0
    half_width = 92.0 + float((index * 5) % 17)
    half_height = 132.0 + float((index * 11) % 29)
    points.extend(
        [
            [center_x - half_width, center_y + half_height],
            [center_x + half_width, center_y + half_height],
            [center_x + half_width, center_y - half_height],
            [center_x - half_width, center_y - half_height],
        ]
    )

    outer = {
        "start": 0,
        "closed": True,
        "segments": [
            {"type": "line", "to": 1},
            {"type": "quadratic", "control": 2, "to": 3},
            {"type": "line", "to": 4},
            {"type": "quadratic", "control": 5, "to": 6},
            {"type": "line", "to": 7},
            {"type": "quadratic", "control": 8, "to": 9},
            {"type": "line", "to": 10},
            {"type": "quadratic", "control": 11, "to": 0},
        ],
    }
    hole = {
        "start": 12,
        "closed": True,
        "segments": [
            {"type": "line", "to": 13},
            {"type": "line", "to": 14},
            {"type": "line", "to": 15},
            {"type": "line", "to": 12},
        ],
    }
    return {
        "id": f"G{index}",
        "advance": float(700 + ((index * 37) % 71)),
        "points": points,
        "contours": [outer, hole],
    }


def build_sample(workspace: Path, sample_dir: str) -> tuple[Path, Path]:
    sample_path = (workspace / sample_dir).resolve()
    sample_path.mkdir(parents=True, exist_ok=True)
    glyphs_path = sample_path / "glyphs.json"
    scene_path = sample_path / "scene.json"

    sizes = [118.0, 126.0, 132.0, 122.0, 142.0, 136.0]
    offsets = [0.0, 0.25, 0.5, 0.75, 0.125, 0.375]
    glyphs = []
    for index in range(6):
        glyph = _rounded_glyph(index)
        glyph["size"] = sizes[index]
        glyph["xOffset"] = offsets[index]
        glyphs.append(glyph)

    outlines = {
        "schema": "glyph-outlines/v1",
        "description": "Deterministic rounded boxes with nonzero-winding holes",
        "glyphs": glyphs,
    }
    glyphs_path.write_text(
        json.dumps(outlines, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    scene = {
        "schema": "glyph-raster-scene/v1",
        "name": "deterministic-six-glyph-sample",
        "outlineFile": f"{sample_dir}/glyphs.json",
        "font": {"unitsPerEm": 1000.0, "baselineOffset": 40.0},
        "layout": {"marginX": 18.0, "spacing": 8.0},
        "resolution": {"width": 720, "height": 240},
        "supersampling": 4,
        "tile": {"width": 48, "height": 48},
        "concurrency": 4,
        "memoryLimitBytes": 110592,
        "stateDir": "state/sample",
        "output": "out/sample.pgm",
        "report": "out/sample-report.json",
        "edits": [
            {
                "op": "set_point",
                "glyph": "G1",
                "point": 3,
                "x": float(glyphs[1]["points"][3][0] + 7.5),
                "y": float(glyphs[1]["points"][3][1] + 4.25),
            },
            {"op": "set_size", "glyph": "G4", "size": 137.5},
            {"op": "set_x_offset", "glyph": "G0", "xOffset": 0.4375},
        ],
    }
    scene_path.write_text(
        json.dumps(scene, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return scene_path, glyphs_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate deterministic local samples")
    parser.add_argument("--workspace", default=".", help="workspace root")
    parser.add_argument("--sample-dir", default="samples", help="sample directory")
    args = parser.parse_args()
    workspace = Path(args.workspace).resolve()
    sample_path = (workspace / args.sample_dir).resolve()
    try:
        sample_path.relative_to(workspace)
    except ValueError:
        parser.error("--sample-dir must resolve inside --workspace")
    scene_path, glyphs_path = build_sample(workspace, args.sample_dir)
    print(
        json.dumps(
            {
                "status": "ok",
                "scene": str(scene_path),
                "glyphs": str(glyphs_path),
                "glyphCount": 6,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
