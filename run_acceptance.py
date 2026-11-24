"""End-to-end acceptance checks against the fixed-seed local sample.

The script exercises every acceptance criterion from the task brief and
prints a compact PASS/FAIL summary.  It never touches the network.
"""
from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path

import numpy as np

from dungeon_render import persistence
from dungeon_render.pipeline import run_pipeline
from dungeon_render.raster import Renderer
from dungeon_render.tiles import build_tiles
from dungeon_render.validation import load_level_and_task


def main() -> int:
    root = Path("work_acceptance")
    shutil.rmtree(root, ignore_errors=True)
    level, task = load_level_and_task(
        "samples/level.json", "samples/render_task.json"
    )
    base = replace(
        task,
        state_dir=str(root / "state"),
        output_dir=str(root / "output"),
    )
    results: list[tuple[str, bool, str]] = []

    def check(name, condition, detail=""):
        results.append((name, bool(condition), detail))

    # 1. Fresh tiled render versus single-tile whole-image reference.
    fresh = run_pipeline(level, base, capture_signals=False)
    whole = fresh["report"]["whole_image"]
    check(
        "tiled assembly pixel-identical to whole render",
        whole["gray_max_delta"] == 0 and whole["outline_max_delta"] == 0,
        json.dumps(whole),
    )
    check(
        "no tile seam steps",
        all(tile["gray_seam_max_delta"] == 0 for tile in fresh["report"]["tiles"])
        and all(
            tile["outline_seam_max_delta"] == 0
            for tile in fresh["report"]["tiles"]
        ),
    )

    # 2. Re-run reuses persisted tiles.
    rerun = run_pipeline(level, base, capture_signals=False)
    check(
        "second run reuses every tile",
        all(tile["status"] == "reused" for tile in rerun["report"]["tiles"]),
    )

    # 3. Determinism across concurrency settings and ragged tile boundaries.
    plans = set()
    for concurrency in (1, 2, 4):
        variant = replace(
            base,
            concurrency=concurrency,
            state_dir=str(root / f"state_c{concurrency}"),
            output_dir=str(root / f"output_c{concurrency}"),
        )
        outcome = run_pipeline(level, variant, capture_signals=False)
        plans.add(json.dumps(outcome["round_plan"]))
        check(
            f"concurrency={concurrency} exact pixels",
            outcome["report"]["whole_image"]["gray_max_delta"] == 0
            and outcome["report"]["whole_image"]["outline_max_delta"] == 0,
        )
    check("admission plan identical across concurrency", len(plans) == 1)

    ragged = replace(
        base,
        tile_cols=10,
        tile_rows=7,
        state_dir=str(root / "state_ragged"),
        output_dir=str(root / "output_ragged"),
        in_flight_pixel_limit=2000,
    )
    ragged_result = run_pipeline(level, ragged, capture_signals=False)
    check(
        "non-cell-aligned tiles remain seam-free",
        ragged_result["report"]["whole_image"]["gray_max_delta"] == 0,
    )

    # 4. Joint budget scheduling: memory cap forcing serial buffering.
    serial_mem = replace(
        base,
        in_flight_pixel_limit=1024,
        state_dir=str(root / "state_mem"),
        output_dir=str(root / "output_mem"),
    )
    mem_result = run_pipeline(level, serial_mem, capture_signals=False)
    check(
        "single-tile memory budget still completes exactly",
        mem_result["report"]["whole_image"]["gray_max_delta"] == 0,
    )

    # 5. Digest corruption triggers E_DIGEST and self-heals.
    gray_files = sorted(Path(base.state_dir).glob("tile_*_gray.npy"))
    target = gray_files[7]
    raw = bytearray(target.read_bytes())
    raw[-1] ^= 0x55
    target.write_bytes(bytes(raw))
    healed = run_pipeline(level, base, capture_signals=False)
    check(
        "corrupt tile reported E_DIGEST then re-rendered",
        "E_DIGEST" in healed["business_codes"]
        and healed["report"]["whole_image"]["gray_max_delta"] == 0,
    )

    # 6. Graceful interrupt at a tile boundary, then resume.
    interrupt_task = replace(
        base,
        state_dir=str(root / "state_interrupt"),
        output_dir=str(root / "output_interrupt"),
    )

    def stop_after_three():
        state = Path(interrupt_task.state_dir)
        return (
            state.exists() and len(list(state.glob("tile_*_gray.npy"))) >= 3
        )

    interrupted = run_pipeline(
        level,
        interrupt_task,
        stop_event=stop_after_three,
        capture_signals=False,
    )
    marker = Path(interrupt_task.state_dir) / persistence.INTERRUPT_FILE
    check(
        "graceful interrupt exits 130 with marker and no outputs",
        interrupted["exit_code"] == 130
        and marker.exists()
        and not (
            Path(interrupt_task.output_dir) / "dungeon_gray.pgm"
        ).exists(),
    )
    resumed = run_pipeline(level, interrupt_task, capture_signals=False)
    check(
        "resume completes and clears marker",
        resumed["exit_code"] == 0
        and not marker.exists()
        and resumed["report"]["whole_image"]["gray_max_delta"] == 0,
    )

    # 7. Version change archives the old state instead of overwriting it.
    changed_level = replace(level, light_radius=8.0)
    version_task = replace(
        base,
        state_dir=str(root / "state_version"),
        output_dir=str(root / "output_version"),
    )
    run_pipeline(level, version_task, capture_signals=False)
    changed = run_pipeline(changed_level, version_task, capture_signals=False)
    archive_root = Path(version_task.state_dir) / persistence.ARCHIVE_DIR
    check(
        "old version archived before writing new tiles",
        bool(changed["report"]["archived_versions"]) and archive_root.exists(),
    )

    # 8. Output PGM headers carry the version and correct dimensions.
    header = persistence.read_pgm_header(
        Path(base.output_dir) / "dungeon_gray.pgm"
    )
    check(
        "published PGM records size and version",
        header["width"] == task.output_width
        and header["height"] == task.output_height
        and bool(header["version"]),
        json.dumps(header),
    )

    width = max(len(name) for name, _, _ in results)
    failures = 0
    for name, ok, detail in results:
        status = "PASS" if ok else "FAIL"
        print(f"{status}  {name.ljust(width)}  {detail}")
        failures += 0 if ok else 1
    print(f"\n{len(results) - failures}/{len(results)} acceptance checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
