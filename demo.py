"""One-command preview-first demo with deterministic local samples."""

from __future__ import annotations

import argparse
import json
import shutil
import time
from pathlib import Path

import numpy as np

from relief_tiles.config import load_config
from relief_tiles.output import save_preview, save_render
from relief_tiles.renderer import ReliefRenderer
from relief_tiles.samples import generate_samples


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default="demo_run")
    parser.add_argument("--regenerate-samples", action="store_true")
    args = parser.parse_args()
    root = Path(args.root)
    samples_dir = Path("samples")
    state_dir = root / "state"
    output_dir = root / "out"
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    if args.regenerate_samples or not (samples_dir / "config.json").exists():
        generate_samples(samples_dir)

    config = load_config(samples_dir / "config.json")
    reference = np.load(samples_dir / "reference_gray.npy", allow_pickle=False)
    renderer = ReliefRenderer(config, str(state_dir))

    started = time.perf_counter()
    first_preview = renderer.preview_block(config.preview_row, config.preview_col)
    first_ms = (time.perf_counter() - started) * 1000.0
    save_preview(first_preview, config, output_dir / "preview")

    started = time.perf_counter()
    second_preview = renderer.preview_block(config.preview_row, config.preview_col)
    second_ms = (time.perf_counter() - started) * 1000.0
    assert second_preview.cached

    result = renderer.render_all(reference=reference)
    paths = save_render(result, config, output_dir)
    exact = bool(np.array_equal(result.mosaic, reference))
    summary = {
        "ok": result.ok and exact,
        "config_version": config.version,
        "preview": {
            "block": [config.preview_row, config.preview_col],
            "first_render_ms": round(first_ms, 3),
            "cached_second_render_ms": round(second_ms, 3),
            "cached": second_preview.cached,
        },
        "render": {
            "recomputed_blocks": result.report["recomputed_blocks"],
            "reused_blocks": result.report["reused_blocks"],
            "seam_max_abs_adjacent_delta": result.report[
                "seam_max_abs_adjacent_delta"
            ],
            "max_abs_diff_vs_full_reference": result.report[
                "max_abs_diff_vs_full_reference"
            ],
            "pixel_exact": exact,
        },
        "outputs": paths,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if summary["ok"] else 4


if __name__ == "__main__":
    raise SystemExit(main())
