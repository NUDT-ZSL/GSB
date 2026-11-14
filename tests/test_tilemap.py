"""Acceptance tests for the tile pyramid generator."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest
from PIL import Image

from tilemap import cli, core
from tilemap.core import compute_max_level, generate_tiles, level_dimensions
from samples.make_sample import (
    build_transparent_half,
    build_world_map,
    write_corrupt_png,
)

TILE = 256
PATTERN = re.compile(r"^(\d+)_(\d+)_(\d+)\.png$")


@pytest.fixture()
def source_png(tmp_path: Path) -> Path:
    path = tmp_path / "world_500x300.png"
    build_world_map().save(path)
    return path


@pytest.fixture()
def transparent_png(tmp_path: Path) -> Path:
    path = tmp_path / "transparent_512x256.png"
    build_transparent_half().save(path)
    return path


def read_manifest(out_dir: Path) -> dict:
    return json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))


def tile_files(out_dir: Path) -> set[str]:
    return {path.name for path in out_dir.glob("*.png") if path.is_file()}


# 1. geometry and naming ----------------------------------------------------


def test_max_level_and_grid_for_500x300():
    assert compute_max_level(500, 300, 256) == 1
    level0 = level_dimensions(500, 300, 256, 0)
    assert (level0.cols, level0.rows) == (2, 2)
    assert (level0.width, level0.height) == (500, 300)

    top = level_dimensions(500, 300, 256, 1)
    assert (top.cols, top.rows) == (1, 1)
    assert (top.width, top.height) == (250, 150)
    assert top.total_tiles == 1


def test_max_level_always_yields_single_tile():
    for width, height, tile in [
        (1, 1, 256),
        (256, 256, 256),
        (257, 1, 256),
        (500, 300, 256),
        (1000, 1000, 64),
        (3333, 1777, 128),
    ]:
        top = compute_max_level(width, height, tile)
        geometry = level_dimensions(width, height, tile, top)
        assert geometry.total_tiles == 1, (width, height, tile)


def test_tile_files_are_named_z_x_y(source_png: Path, tmp_path: Path):
    out_dir = tmp_path / "tiles"
    manifest = generate_tiles(source_png, out_dir, tile_size=TILE)

    expected = {"0_0_0.png", "0_0_1.png", "0_1_0.png", "0_1_1.png", "1_0_0.png"}
    assert tile_files(out_dir) == expected
    assert all(PATTERN.match(name) for name in tile_files(out_dir))

    names_in_manifest = {
        tile["path"]
        for level in manifest["levels"]
        for tile in level["tiles"]
    }
    assert names_in_manifest == expected


# 2. pixel cropping ---------------------------------------------------------


def test_bottom_right_tile_pads_with_transparency(source_png: Path, tmp_path: Path):
    out_dir = tmp_path / "tiles"
    generate_tiles(source_png, out_dir, tile_size=TILE)
    source = Image.open(source_png).convert("RGBA")

    tile = Image.open(out_dir / "0_1_1.png").convert("RGBA")
    assert tile.size == (TILE, TILE)
    assert source.size == (500, 300)
    covered_w, covered_h = 500 - 256, 300 - 256  # 244 x 44

    # Covered area must match the source exactly, pixel for pixel.
    assert tile.crop((0, 0, covered_w, covered_h)).tobytes() == (
        source.crop((256, 256, 500, 300)).tobytes()
    )
    # Everything outside the source is fully transparent.
    right_pad = tile.crop((covered_w, 0, TILE, TILE)).getchannel("A")
    bottom_pad = tile.crop((0, covered_h, TILE, TILE)).getchannel("A")
    assert right_pad.getbbox() is None
    assert bottom_pad.getbbox() is None


def test_every_tile_matches_its_own_region_only(source_png: Path, tmp_path: Path):
    out_dir = tmp_path / "tiles"
    generate_tiles(source_png, out_dir, tile_size=TILE)
    source = Image.open(source_png).convert("RGBA")

    for name in ("0_0_0.png", "0_1_0.png", "0_0_1.png"):
        _, x, y = PATTERN.match(name).groups()
        x, y = int(x), int(y)
        left, top = x * TILE, y * TILE
        expected = source.crop(
            (
                left,
                top,
                min(left + TILE, source.width),
                min(top + TILE, source.height),
            )
        )
        tile = Image.open(out_dir / name).convert("RGBA")
        assert tile.crop((0, 0, expected.width, expected.height)).tobytes() == (
            expected.tobytes()
        ), name

    # A known transparent hole in the source stays transparent and does not
    # pull in colour from neighbouring tiles.
    hole = Image.open(out_dir / "0_0_0.png").convert("RGBA").crop((20, 20, 91, 91))
    assert hole.getchannel("A").getbbox() is None


def test_non_divisible_source_geometry(tmp_path: Path):
    path = tmp_path / "odd.png"
    image = Image.new("RGBA", (500, 300), (10, 20, 30, 255))
    for x in range(0, 500, 7):
        for y in range(0, 300, 11):
            image.putpixel((x, y), (200, 100, 50, 255))
    image.save(path)

    out_dir = tmp_path / "odd_out"
    manifest = generate_tiles(path, out_dir, tile_size=100)
    level0 = manifest["levels"][0]
    assert (level0["cols"], level0["rows"]) == (5, 3)
    assert level0["total"] == 15
    assert len(tile_files(out_dir)) == manifest["summary"]["total"]

    last = Image.open(out_dir / "0_4_2.png")
    assert last.size == (100, 100)


# 3. empty tiles ------------------------------------------------------------


def test_fully_transparent_tile_is_created_and_marked_empty(
    transparent_png: Path, tmp_path: Path
):
    out_dir = tmp_path / "tiles"
    manifest = generate_tiles(transparent_png, out_dir, tile_size=TILE)

    empty_path = out_dir / "0_0_0.png"
    assert empty_path.is_file()
    tile = Image.open(empty_path).convert("RGBA")
    assert tile.size == (TILE, TILE)
    assert tile.getchannel("A").getbbox() is None

    entry = {
        tile["path"]: tile
        for level in manifest["levels"]
        for tile in level["tiles"]
    }["0_0_0.png"]
    assert entry["status"] == "empty"
    assert entry["empty"] is True
    assert entry["error"] is None
    assert manifest["levels"][0]["empty"] == 1
    # An empty tile still counts as successfully generated.
    assert manifest["levels"][0]["success"] == manifest["levels"][0]["total"]


def test_level_zero_alpha_matches_source(transparent_png: Path, tmp_path: Path):
    out_dir = tmp_path / "tiles"
    generate_tiles(transparent_png, out_dir, tile_size=TILE)
    source = Image.open(transparent_png).convert("RGBA")
    tile = Image.open(out_dir / "0_0_0.png").convert("RGBA")
    assert tile.tobytes() == source.crop((0, 0, TILE, TILE)).tobytes()


# 4. manifest ---------------------------------------------------------------


def test_manifest_fields_and_ordering(source_png: Path, tmp_path: Path):
    out_dir = tmp_path / "tiles"
    manifest = generate_tiles(source_png, out_dir, tile_size=TILE)

    for key in ("source", "tile_size", "max_level", "start_level", "levels", "summary"):
        assert key in manifest
    assert manifest["source"]["width"] == 500
    assert manifest["source"]["height"] == 300
    assert manifest["source"]["format"] == "PNG"
    assert manifest["source"]["path"].endswith("world_500x300.png")
    assert manifest["tile_size"] == TILE

    assert [level["level"] for level in manifest["levels"]] == [0, 1]
    for level in manifest["levels"]:
        for key in ("level", "cols", "rows", "total", "success", "empty", "failed"):
            assert key in level
        assert len(level["tiles"]) == level["total"] == level["cols"] * level["rows"]
        assert level["total"] == level["success"] + level["failed"]
        assert level["success"] == level["ok"] + level["empty"]

        coords = [(tile["y"], tile["x"]) for tile in level["tiles"]]
        assert coords == sorted(coords), "tiles must be row-major"
        for tile in level["tiles"]:
            for key in ("level", "x", "y", "path", "status", "source_region"):
                assert key in tile
            assert tile["level"] == level["level"]
            assert (out_dir / tile["path"]).is_file()
            region = tile["source_region"]
            assert region["x"] == tile["x"] * TILE * (1 << level["level"])
            assert region["y"] == tile["y"] * TILE * (1 << level["level"])
            assert region["width"] == TILE * (1 << level["level"])

    assert manifest["summary"]["total"] == 5
    assert manifest["summary"]["failed"] == 0


def test_manifest_is_deterministic(source_png: Path, tmp_path: Path):
    first = tmp_path / "run1"
    second = tmp_path / "run2"
    generate_tiles(source_png, first, tile_size=TILE)
    generate_tiles(source_png, second, tile_size=TILE)

    assert read_manifest(first) == read_manifest(second)
    for name in tile_files(first):
        assert (first / name).read_bytes() == (second / name).read_bytes(), name


# 5. per-tile failure isolation ---------------------------------------------


def test_tile_failure_is_isolated_and_reported(source_png: Path, tmp_path: Path):
    out_dir = tmp_path / "tiles"
    out_dir.mkdir()
    # A directory in place of a tile file makes that single write fail while
    # leaving the output directory writable for every other tile.
    (out_dir / "0_1_0.png").mkdir()

    failures: list[dict] = []
    manifest = generate_tiles(
        source_png, out_dir, tile_size=TILE, on_tile_error=failures.append
    )

    level0 = manifest["levels"][0]
    failed = [tile for tile in level0["tiles"] if tile["status"] == "failed"]
    assert len(failed) == 1
    assert failed[0]["path"] == "0_1_0.png"
    assert failed[0]["status"] != "ok"
    assert failed[0]["error"]

    assert level0["failed"] == 1
    assert level0["success"] == 3
    assert level0["total"] == 4
    assert manifest["summary"]["failed"] == 1

    # Every other tile was still written, and the top level too.
    assert tile_files(out_dir) == {
        "0_0_0.png",
        "0_0_1.png",
        "0_1_1.png",
        "1_0_0.png",
    }
    for name in ("0_0_0.png", "0_0_1.png", "0_1_1.png", "1_0_0.png"):
        assert Image.open(out_dir / name).size == (TILE, TILE)
    assert failures == failed


@pytest.mark.skipif(
    os.name != "nt",
    reason=(
        "simulates a partially unwritable output with read-only tile files; "
        "POSIX rename replaces read-only files, so the same failure cannot be "
        "reproduced there"
    ),
)
def test_read_only_targets_keep_generating(source_png: Path, tmp_path: Path):
    out_dir = tmp_path / "ro"
    out_dir.mkdir()
    locked = [out_dir / "0_0_1.png", out_dir / "1_0_0.png"]
    for path in locked:
        path.write_bytes(b"stale")
        os.chmod(path, 0o444)  # read-only: the tile write must fail
    try:
        failures: list[dict] = []
        manifest = generate_tiles(
            source_png, out_dir, tile_size=TILE, on_tile_error=failures.append
        )
    finally:
        for path in locked:
            os.chmod(path, 0o666)

    assert {tile["path"] for tile in failures} == {"0_0_1.png", "1_0_0.png"}
    assert all(tile["error"] for tile in failures)
    assert manifest["summary"]["failed"] == 2
    assert manifest["summary"]["success"] == 3

    # Untouched tiles are byte-identical to a clean run, and the locked files
    # keep their previous content instead of being truncated.
    clean = tmp_path / "clean"
    generate_tiles(source_png, clean, tile_size=TILE)
    for name in ("0_0_0.png", "0_1_0.png", "0_1_1.png"):
        assert (out_dir / name).read_bytes() == (clean / name).read_bytes()
    for path in locked:
        assert path.read_bytes() == b"stale"

    # No stray temporary files survive the failed writes.
    assert not list(out_dir.glob(".*.tmp"))

    manifest_on_disk = read_manifest(out_dir)
    assert manifest_on_disk["summary"]["failed"] == 2


def test_manifest_write_failure_is_fatal(source_png: Path, tmp_path: Path):
    out_dir = tmp_path / "tiles"
    blocker = tmp_path / "manifest_blocker"
    blocker.mkdir()

    # The manifest target is an existing directory, so the atomic replace
    # cannot succeed and the run must fail loudly instead of silently losing
    # the manifest.
    with pytest.raises(core.ManifestError):
        generate_tiles(
            source_png, out_dir, tile_size=TILE, manifest_path=blocker
        )


# 6. start level ------------------------------------------------------------


def test_start_level_skips_lower_levels(source_png: Path, tmp_path: Path):
    out_dir = tmp_path / "tiles"
    manifest = generate_tiles(source_png, out_dir, tile_size=TILE, start_level=1)

    assert tile_files(out_dir) == {"1_0_0.png"}
    level0 = manifest["levels"][0]
    assert level0["skipped"] is True
    assert level0["tiles"] == []
    assert level0["total"] == 0
    assert level0["cols"] == 2 and level0["rows"] == 2
    assert manifest["levels"][1]["skipped"] is False
    assert manifest["start_level"] == 1
    assert manifest["summary"]["total"] == 1


def test_start_level_out_of_range_is_rejected(source_png: Path, tmp_path: Path):
    with pytest.raises(core.ConfigError):
        generate_tiles(source_png, tmp_path / "x", start_level=99)
    assert not (tmp_path / "x").exists()


# 7. stdout progress --------------------------------------------------------


def test_cli_prints_per_level_counts_matching_manifest(
    source_png: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    out_dir = tmp_path / "tiles"
    assert cli.main([str(source_png), str(out_dir), "--tile-size", str(TILE)]) == 0
    stdout = capsys.readouterr().out

    manifest = read_manifest(out_dir)
    lines = [line for line in stdout.splitlines() if line.startswith("level ")]
    assert len(lines) == len(manifest["levels"])

    for line, level in zip(lines, manifest["levels"], strict=True):
        match = re.search(
            r"level (\d+): total=(\d+) success=(\d+) empty=(\d+) failed=(\d+)",
            line,
        )
        assert match, line
        got = tuple(int(value) for value in match.groups())
        assert got == (
            level["level"],
            level["total"],
            level["success"],
            level["empty"],
            level["failed"],
        )
    assert "summary:" in stdout


def test_cli_prints_skipped_level(
    source_png: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    out_dir = tmp_path / "tiles"
    code = cli.main(
        [str(source_png), str(out_dir), "--start-level", "1"]
    )
    assert code == 0
    stdout = capsys.readouterr().out
    assert "level 0: skipped" in stdout
    assert "level 1: total=1 success=1" in stdout


# 8. fatal source errors ----------------------------------------------------


def test_corrupt_png_aborts_without_output(tmp_path: Path, capsys):
    bad = tmp_path / "corrupt.png"
    write_corrupt_png(bad)
    out_dir = tmp_path / "tiles"

    code = cli.main([str(bad), str(out_dir)])
    captured = capsys.readouterr()

    assert code != 0
    assert "error:" in captured.err
    assert not out_dir.exists() or not any(out_dir.iterdir())
    assert not (out_dir / "manifest.json").exists()


def test_unsupported_format_aborts(tmp_path: Path):
    source = tmp_path / "image.tga"
    build_world_map(64, 64).save(source)
    out_dir = tmp_path / "tiles"

    with pytest.raises(core.SourceError):
        generate_tiles(source, out_dir)
    assert not out_dir.exists()


def test_missing_source_aborts(tmp_path: Path):
    out_dir = tmp_path / "tiles"
    with pytest.raises(core.SourceError):
        generate_tiles(tmp_path / "nope.png", out_dir)
    assert not out_dir.exists()


def test_pixel_guard_rejects_huge_source_before_writing(source_png: Path, tmp_path: Path):
    out_dir = tmp_path / "tiles"
    with pytest.raises(core.SourceError):
        generate_tiles(source_png, out_dir, max_pixels=1000)
    assert not out_dir.exists()

    # Disabling the guard lets the same image through.
    manifest = generate_tiles(
        source_png, out_dir, tile_size=TILE, max_pixels=0
    )
    assert manifest["summary"]["failed"] == 0


def test_non_png_source_still_produces_png_tiles(tmp_path: Path):
    source = tmp_path / "photo.jpg"
    build_world_map(300, 200).convert("RGB").save(source, quality=90)
    out_dir = tmp_path / "tiles"
    manifest = generate_tiles(source, out_dir, tile_size=TILE)
    assert manifest["source"]["format"] == "JPEG"
    for name in tile_files(out_dir):
        with Image.open(out_dir / name) as tile:
            assert tile.format == "PNG"


# CLI argument validation ---------------------------------------------------


def test_cli_rejects_bad_tile_size(source_png: Path, tmp_path: Path):
    with pytest.raises(SystemExit) as excinfo:
        cli.main([str(source_png), str(tmp_path / "out"), "--tile-size", "0"])
    assert excinfo.value.code == 2
