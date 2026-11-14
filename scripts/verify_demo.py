"""End-to-end acceptance demo for the tile pyramid generator.

Run from the project root::

    python scripts/verify_demo.py

The script regenerates the deterministic sample images, drives the CLI as a
user would, checks every acceptance criterion from the brief, and prints a
verification record. Exit code is 0 only when all checks pass.

Nothing outside the project directory is touched.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from samples.make_sample import (  # noqa: E402
    build_transparent_half,
    build_world_map,
    main as write_samples,
    write_corrupt_png,
)

SAMPLE = ROOT / "samples"
WORK = ROOT / "out" / "verify"
TILE = 256
PYTHON = sys.executable

CHECKS: list[tuple[str, bool, str]] = []


def check(check_id: str, ok: bool, detail: str) -> None:
    CHECKS.append((check_id, ok, detail))
    print(f"[{check_id}] {'PASS' if ok else 'FAIL'} {detail}", flush=True)


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [PYTHON, "-m", "tilemap", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def read_manifest(out_dir: Path) -> dict:
    return json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))


def png_names(out_dir: Path) -> set[str]:
    return {p.name for p in out_dir.glob("*.png") if p.is_file()}


def reset(path: Path) -> Path:
    """Remove a generated directory, clearing read-only flags first.

    Windows keeps read-only files undeletable, so repeated runs would
    otherwise fail on the leftover locked tile from the failure scenario.
    """
    if path.exists():
        for item in [path, *path.rglob("*")]:
            try:
                item.chmod(0o666)
            except OSError:
                pass
        shutil.rmtree(path)
    return path


def digest_tree(out_dir: Path) -> dict[str, str]:
    return {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(out_dir.glob("*"))
        if p.is_file()
    }


def scenario_main() -> None:
    """Checks 1, 2, 4, 7 and determinism on the 500x300 sample."""
    out_dir = reset(WORK / "main")
    result = run_cli(
        str(SAMPLE / "world_500x300.png"), str(out_dir), "--tile-size", str(TILE)
    )
    print("--- CLI output (main sample) ---")
    print(result.stdout.rstrip())
    print("--- end CLI output ---", flush=True)

    ok = result.returncode == 0 and (out_dir / "manifest.json").is_file()
    check("run", ok, f"exit={result.returncode}")

    manifest = read_manifest(out_dir)
    lvl0 = manifest["levels"][0]

    # 1. geometry + naming
    expected_names = {
        "0_0_0.png",
        "0_0_1.png",
        "0_1_0.png",
        "0_1_1.png",
        "1_0_0.png",
    }
    top = manifest["levels"][-1]
    check(
        "1-geometry",
        (lvl0["cols"], lvl0["rows"]) == (2, 2)
        and (top["cols"], top["rows"]) == (1, 1)
        and manifest["max_level"] == 1
        and png_names(out_dir) == expected_names,
        f"level0={lvl0['cols']}x{lvl0['rows']} tiles, "
        f"top=level{top['level']} {top['cols']}x{top['rows']} tile, "
        f"files={sorted(png_names(out_dir))}",
    )

    # 2. pixel fidelity / transparent padding
    source = Image.open(SAMPLE / "world_500x300.png").convert("RGBA")
    tile = Image.open(out_dir / "0_1_1.png").convert("RGBA")
    covered_w, covered_h = 500 - TILE, 300 - TILE
    pixels_match = tile.crop((0, 0, covered_w, covered_h)).tobytes() == (
        source.crop((TILE, TILE, 500, 300)).tobytes()
    )
    pad_right = tile.crop((covered_w, 0, TILE, TILE)).getchannel("A")
    pad_bottom = tile.crop((0, covered_h, TILE, TILE)).getchannel("A")
    padded = pad_right.getbbox() is None and pad_bottom.getbbox() is None
    check(
        "2-crop-pad",
        tile.size == (TILE, TILE) and pixels_match and padded,
        f"0_1_1.png size={tile.size}, covered {covered_w}x{covered_h} px "
        f"match source={pixels_match}, alpha outside source all 0={padded}",
    )

    # no neighbouring-region leakage on the other tiles
    leaks = []
    for x, y in ((0, 0), (1, 0), (0, 1)):
        name = f"0_{x}_{y}.png"
        region = source.crop(
            (x * TILE, y * TILE, min((x + 1) * TILE, 500), min((y + 1) * TILE, 300))
        )
        got = Image.open(out_dir / name).convert("RGBA").crop(
            (0, 0, region.width, region.height)
        )
        if got.tobytes() != region.tobytes():
            leaks.append(name)
    check("2-no-leak", not leaks, f"tiles matching own source region only, bad={leaks}")

    # 4. manifest completeness and ordering
    complete = True
    details = []
    for level in manifest["levels"]:
        coords = [(t["y"], t["x"]) for t in level["tiles"]]
        if coords != sorted(coords):
            complete = False
            details.append(f"level{level['level']} ordering")
        if len(level["tiles"]) != level["cols"] * level["rows"]:
            complete = False
            details.append(f"level{level['level']} tile count")
        for t in level["tiles"]:
            path = out_dir / t["path"]
            if not path.is_file() or not all(
                key in t
                for key in ("level", "x", "y", "path", "status", "source_region")
            ):
                complete = False
                details.append(f"level{level['level']} tile {t}")
    required = {"source", "tile_size", "max_level", "start_level", "levels", "summary"}
    if not required <= manifest.keys():
        complete = False
        details.append(f"missing top-level fields {sorted(required - manifest.keys())}")
    check(
        "4-manifest",
        complete,
        f"fields={sorted(manifest.keys())}, "
        f"manifest tiles={manifest['summary']['total']}, disk pngs={len(png_names(out_dir))}, "
        f"problems={details}",
    )

    # 7. stdout counts match manifest
    lines = [l for l in result.stdout.splitlines() if l.startswith("level ")]
    stdout_ok = len(lines) == len(manifest["levels"])
    parsed = []
    for line, level in zip(lines, manifest["levels"]):
        fields = dict(
            item.split("=", 1) for item in line.split(": ", 1)[1].split(" ")
        )
        parsed.append(fields)
        stdout_ok &= int(fields["total"]) == level["total"]
        stdout_ok &= int(fields["success"]) == level["success"]
    check(
        "7-stdout",
        stdout_ok,
        "stdout " + ", ".join(
            f"level{lv['level']}:{f['total']}/{f['success']}"
            for lv, f in zip(manifest["levels"], parsed)
        ),
    )

    # determinism
    second = reset(WORK / "main-repeat")
    result2 = run_cli(
        str(SAMPLE / "world_500x300.png"), str(second), "--tile-size", str(TILE)
    )
    first_hash = digest_tree(out_dir)
    second_hash = digest_tree(second)
    check(
        "determinism",
        result2.returncode == 0 and first_hash == second_hash,
        f"{len(first_hash)} files byte-identical across runs, "
        f"manifest sha256={first_hash['manifest.json'][:16]}",
    )


def scenario_empty() -> None:
    """Check 3: fully transparent tiles are still written and marked empty."""
    out_dir = reset(WORK / "transparent")
    result = run_cli(
        str(SAMPLE / "transparent_half_512x256.png"),
        str(out_dir),
        "--tile-size",
        str(TILE),
    )
    print("--- CLI output (transparent sample) ---")
    print(result.stdout.rstrip())
    print("--- end CLI output ---", flush=True)

    manifest = read_manifest(out_dir)
    level0 = manifest["levels"][0]
    entry = next(t for t in level0["tiles"] if t["path"] == "0_0_0.png")
    tile_path = out_dir / "0_0_0.png"
    tile = Image.open(tile_path).convert("RGBA")
    check(
        "3-empty",
        tile_path.is_file()
        and tile.size == (TILE, TILE)
        and tile.getchannel("A").getbbox() is None
        and entry["status"] == "empty"
        and entry["empty"] is True
        and level0["empty"] == 1,
        f"0_0_0.png exists size={tile.size} alpha-empty, "
        f"manifest status={entry['status']}, level0 empty count={level0['empty']}",
    )


def scenario_tile_failure() -> None:
    """Check 5: one unwritable tile does not stop the rest."""
    out_dir = reset(WORK / "failure")
    out_dir.mkdir(parents=True)
    locked = out_dir / "0_1_0.png"
    locked.write_bytes(b"stale")
    os.chmod(locked, 0o444)  # read-only target file

    result = run_cli(
        str(SAMPLE / "world_500x300.png"), str(out_dir), "--tile-size", str(TILE)
    )
    print("--- CLI output (locked tile) ---")
    print(result.stdout.rstrip())
    print("--- end CLI output ---")
    print("stderr:", result.stderr.strip(), flush=True)

    manifest = read_manifest(out_dir)
    level0 = manifest["levels"][0]
    failed = [t for t in level0["tiles"] if t["status"] == "failed"]
    produced = png_names(out_dir)
    untouched_ok = all(
        (out_dir / name).is_file()
        for name in ("0_0_0.png", "0_0_1.png", "0_1_1.png", "1_0_0.png")
    )
    check(
        "5-isolated-failure",
        result.returncode == 0
        and len(failed) == 1
        and failed[0]["path"] == "0_1_0.png"
        and bool(failed[0]["error"])
        and failed[0]["status"] != "ok"
        and untouched_ok
        and level0["success"] == 3
        and level0["failed"] == 1
        and locked.read_bytes() == b"stale",
        f"failed={[(t['path'], t['status'], t['error'][:40]) for t in failed]}, "
        f"other tiles present={untouched_ok}, "
        f"level0 total={level0['total']} success={level0['success']} failed={level0['failed']}, "
        f"files on disk={sorted(produced)}",
    )


def scenario_start_level() -> None:
    """Check 6: lower levels are skipped, not written."""
    out_dir = reset(WORK / "start1")
    result = run_cli(
        str(SAMPLE / "world_500x300.png"),
        str(out_dir),
        "--tile-size",
        str(TILE),
        "--start-level",
        "1",
    )
    print("--- CLI output (start-level 1) ---")
    print(result.stdout.rstrip())
    print("--- end CLI output ---", flush=True)

    manifest = read_manifest(out_dir)
    level0 = manifest["levels"][0]
    check(
        "6-start-level",
        png_names(out_dir) == {"1_0_0.png"}
        and level0["skipped"] is True
        and level0["tiles"] == []
        and level0["total"] == 0
        and not any(name.startswith("0_") for name in png_names(out_dir)),
        f"files={sorted(png_names(out_dir))}, level0 skipped={level0['skipped']} "
        f"tiles={len(level0['tiles'])} total={level0['total']}",
    )


def scenario_fatal_source() -> None:
    """Check 8: corrupt PNG aborts before writing anything."""
    bad = SAMPLE / "corrupt.png"
    write_corrupt_png(bad)
    out_dir = WORK / "corrupt-out"
    reset(out_dir)
    result = run_cli(str(bad), str(out_dir))
    print("--- CLI output (corrupt source) ---")
    print(f"exit={result.returncode}")
    print("stderr:", result.stderr.strip(), flush=True)

    check(
        "8-corrupt",
        result.returncode != 0
        and result.stderr.strip() != ""
        and not out_dir.exists(),
        f"exit={result.returncode}, output dir created={out_dir.exists()}",
    )

    tga = SAMPLE / "unsupported.tga"
    tga_dir = reset(WORK / "tga-out")
    result2 = run_cli(str(tga), str(tga_dir))
    check(
        "8-unsupported",
        result2.returncode != 0
        and not tga_dir.exists(),
        f"exit={result2.returncode}, stderr={result2.stderr.strip()[:80]!r}, "
        f"output dir created={tga_dir.exists()}",
    )


def main() -> int:
    # Clear checked-out work output first so repeated runs are byte-identical.
    reset(WORK)
    WORK.mkdir(parents=True, exist_ok=True)
    print("=== regenerating deterministic samples ===")
    write_samples()
    # Keep the demo self-contained even if samples/ was ignored/cleaned.
    for name, builder in (
        ("world_500x300.png", build_world_map),
        ("transparent_half_512x256.png", build_transparent_half),
    ):
        builder().save(SAMPLE / name)

    print("\n=== scenario: main pyramid (checks 1, 2, 4, 7) ===")
    scenario_main()
    print("\n=== scenario: transparent region (check 3) ===")
    scenario_empty()
    print("\n=== scenario: tile write failure (check 5) ===")
    scenario_tile_failure()
    print("\n=== scenario: start-level skip (check 6) ===")
    scenario_start_level()
    print("\n=== scenario: fatal source errors (check 8) ===")
    scenario_fatal_source()

    failures = [c for c in CHECKS if not c[1]]
    print(f"\n=== verification summary: {len(CHECKS) - len(failures)}/{len(CHECKS)} passed ===")
    for check_id, ok, detail in CHECKS:
        print(f"  {check_id:24s} {'PASS' if ok else 'FAIL'}")
    if failures:
        print("\nFAILED CHECKS:")
        for check_id, _, detail in failures:
            print(f"  {check_id}: {detail}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
