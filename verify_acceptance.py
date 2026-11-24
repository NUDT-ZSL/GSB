"""Run the deterministic preview, exactness and digest-recovery acceptance flow."""

from __future__ import annotations

import argparse
import json
import shutil
import time
from pathlib import Path
from typing import Any

import numpy as np

from relief_tiles.config import load_config
from relief_tiles.output import save_preview, save_render
from relief_tiles.renderer import ReliefRenderer
from relief_tiles.samples import generate_samples


def _rewrite_corrupt_block(path: Path) -> None:
    with np.load(path, allow_pickle=False) as archive:
        gray = np.asarray(archive["gray"]).copy()
        metadata = np.asarray(archive["metadata"], dtype=np.uint8).tobytes()
    gray[0, 0] ^= 1
    np.savez(
        path,
        gray=gray,
        metadata=np.frombuffer(metadata, dtype=np.uint8),
    )


def run(root: Path) -> dict[str, Any]:
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    samples = root / "samples"
    state = root / "state"
    output = root / "out"
    generate_samples(samples)
    config = load_config(samples / "config.json")
    reference = np.load(samples / "reference_gray.npy", allow_pickle=False)
    renderer = ReliefRenderer(config, str(state))

    started = time.perf_counter()
    preview = renderer.preview_block(config.preview_row, config.preview_col)
    first_preview_ms = (time.perf_counter() - started) * 1000.0
    save_preview(preview, config, output / "preview")

    started = time.perf_counter()
    cached_preview = renderer.preview_block(config.preview_row, config.preview_col)
    cached_preview_ms = (time.perf_counter() - started) * 1000.0

    result = renderer.render_all(reference=reference)
    save_render(result, config, output)
    if not np.array_equal(result.mosaic, reference):
        raise AssertionError("first assembled render is not pixel-exact")

    block_path = (
        state
        / "versions"
        / config.version
        / "blocks"
        / "r0000_c0000.npz"
    )
    _rewrite_corrupt_block(block_path)
    recovered = ReliefRenderer(config, str(state)).render_all(reference=reference)
    save_render(recovered, config, output / "recovered")
    if not np.array_equal(recovered.mosaic, reference):
        raise AssertionError("recovered assembled render is not pixel-exact")
    invalid = {
        (item["row"], item["col"]): item["reason"]
        for item in recovered.report["invalid_blocks"]
    }
    if invalid.get((0, 0)) != "digest_mismatch":
        raise AssertionError("corrupt block was not diagnosed as E_DIGEST")

    summary: dict[str, Any] = {
        "ok": True,
        "python": __import__("sys").version.splitlines()[0],
        "numpy": np.__version__,
        "config_version": config.version,
        "sample_files": sorted(path.name for path in samples.iterdir()),
        "preview": {
            "block": [config.preview_row, config.preview_col],
            "first_ms": round(first_preview_ms, 3),
            "cached_ms": round(cached_preview_ms, 3),
            "cached": cached_preview.cached,
        },
        "first_render": {
            "recomputed_blocks": result.report["recomputed_blocks"],
            "reused_blocks": result.report["reused_blocks"],
            "seam_max_abs_adjacent_delta": result.report[
                "seam_max_abs_adjacent_delta"
            ],
            "max_abs_diff_vs_full_reference": result.report[
                "max_abs_diff_vs_full_reference"
            ],
            "pixel_exact": True,
        },
        "digest_recovery": {
            "invalid_blocks": recovered.report["invalid_blocks"],
            "recomputed_blocks": recovered.report["recomputed_blocks"],
            "max_abs_diff_vs_full_reference": recovered.report[
                "max_abs_diff_vs_full_reference"
            ],
            "pixel_exact": True,
        },
    }
    (root / "acceptance.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default="acceptance_run")
    args = parser.parse_args()
    summary = run(Path(args.root))
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
