"""End-to-end checks for layer_compositor against the local samples.

Run with::

    python -m pytest tests -q

Each test regenerates samples into a temporary directory (deterministically)
and drives the real CLI entry point, so nothing depends on pre-baked output.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest
from PIL import Image

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import layer_compositor  # noqa: E402
import samples.generate_samples as sample_gen  # noqa: E402


@pytest.fixture(scope="session")
def sample_root(tmp_path_factory) -> str:
    root = str(tmp_path_factory.mktemp("samples"))
    # Regenerate deterministically into the temp dir rather than the repo copy.
    sample_gen.main(["--root", root])
    return root


def run_sample(sample_root: str, name: str, out_dir: str) -> dict:
    report = layer_compositor.run(
        os.path.join(sample_root, name, "manifest.json"),
        os.path.join(sample_root, name, "layers"),
        out_dir,
    )
    with open(os.path.join(out_dir, "report.json"), encoding="utf-8") as handle:
        on_disk = json.load(handle)
    assert on_disk == report
    return report


def load_preview(out_dir: str) -> Image.Image:
    with Image.open(os.path.join(out_dir, "preview.png")) as img:
        return img.convert("RGBA")


def layer_by_id(report: dict, layer_id: str) -> dict:
    matches = [layer for layer in report["layers"] if layer["id"] == layer_id]
    assert matches, f"no layer {layer_id!r} in report"
    return matches[0]


# ---------------------------------------------------------------------------
# 1. stacking order
# ---------------------------------------------------------------------------


def test_order_stacking_and_report_order(sample_root, tmp_path):
    report = run_sample(sample_root, "order", str(tmp_path))
    # Manifest lists c, b, a; the report must follow compositing order b, a, c.
    assert [layer["id"] for layer in report["layers"]] == ["b", "a", "c"]
    assert [layer["order"] for layer in report["layers"]] == [1, 2, 3]

    preview = load_preview(str(tmp_path))
    assert preview.size == (80, 80)
    # b is 60x60, a is 40x40, c is 20x20, all centred.
    assert preview.getpixel((5, 5)) == (0, 0, 0, 0)
    assert preview.getpixel((12, 12)) == (40, 160, 220, 255)   # only b
    assert preview.getpixel((22, 22)) == (220, 40, 40, 255)    # a covers b
    assert preview.getpixel((40, 40)) == (240, 200, 30, 255)   # c covers a


# ---------------------------------------------------------------------------
# 2. blend modes
# ---------------------------------------------------------------------------


def _expected_multiply_pixel(base: tuple[int, int, int],
                             top: tuple[int, int, int]) -> tuple[int, int, int]:
    """Hand calculation, rounded half up, for opaque multiply."""
    return tuple((b * t + 127) // 255 for b, t in zip(base, top))


def test_blend_modes_normal_and_multiply(sample_root, tmp_path):
    report = run_sample(sample_root, "blend", str(tmp_path))
    preview = load_preview(str(tmp_path))

    mult = layer_by_id(report, "mult")
    assert mult["blendMode"] == "multiply"
    # base quadrant 1 is (200,120,60), multiply source is (128,255,64).
    assert preview.getpixel((5, 5)) == (
        *_expected_multiply_pixel((200, 120, 60), (128, 255, 64)), 255)

    unknown = layer_by_id(report, "unknown")
    assert unknown["blendMode"] == "normal"
    assert unknown["blendModeFallback"] is True
    assert unknown["blendModeRequested"] == "screen"
    assert preview.getpixel((25, 5)) == (10, 200, 120, 255)

    # Untouched base areas stay base.
    assert preview.getpixel((5, 30)) == (90, 180, 210, 160)


def test_blend_semi_transparent_base_matches_hand_calc(sample_root, tmp_path):
    """Source-over maths on a translucent base matches the W3C formula."""
    report = run_sample(sample_root, "blend", str(tmp_path))
    preview = load_preview(str(tmp_path))
    # Quadrant (0,1) is (90,180,210,160) outside the 20x20 patches; the layer
    # itself is opaque, so the preview must show exactly the base values.
    assert preview.getpixel((25, 25)) == (90, 180, 210, 160)

    base_rgba = (90, 180, 210, 160)
    top_rgba = (128, 255, 64, 255)

    # W3C formula with a fully opaque source:
    #   co = (1-ab)·Cs + ab·B(Cb,Cs)
    # ab = 160/255; B multiplies straight-alpha colour channels.
    ab = 160 / 255
    blended = _expected_multiply_pixel(base_rgba[:3], top_rgba[:3])
    hand_calc = tuple(
        round((1 - ab) * src + ab * blend)
        for src, blend in zip(top_rgba[:3], blended)
    )
    multiply_expected = layer_compositor.composite_pixel(
        base_rgba, top_rgba, "multiply", 100
    )
    assert all(abs(got - want) <= 1 for got, want in
               zip(multiply_expected[:3], hand_calc)), (multiply_expected, hand_calc)
    assert multiply_expected[3] == 255

    normal_expected = layer_compositor.composite_pixel(
        base_rgba, top_rgba, "normal", 100
    )
    assert normal_expected == (128, 255, 64, 255)


# ---------------------------------------------------------------------------
# 3. opacity clamping
# ---------------------------------------------------------------------------


def test_opacity_clamped_and_reported(sample_root, tmp_path):
    report = run_sample(sample_root, "opacity", str(tmp_path))

    high = layer_by_id(report, "high")
    assert high["opacityRequested"] == 150
    assert high["opacity"] == 100
    assert high["opacityClamped"] is True
    assert any("150" in note and "100" in note for note in high["notes"])

    low = layer_by_id(report, "low")
    assert low["opacityRequested"] == -10
    assert low["opacity"] == 0
    assert low["opacityClamped"] is True
    assert low["contributedPixels"] == 0

    preview = load_preview(str(tmp_path))
    # "high" is clamped to 100 and fully covers "under"; "low" is invisible.
    assert preview.getpixel((15, 15)) == (30, 180, 60, 255)


def test_opacity_clamp_helper_bounds():
    assert layer_compositor.clamp_opacity(150) == 100
    assert layer_compositor.clamp_opacity(-10) == 0
    assert layer_compositor.clamp_opacity(0) == 0
    assert layer_compositor.clamp_opacity(100) == 100
    assert layer_compositor.clamp_opacity(55) == 55


# ---------------------------------------------------------------------------
# 4. canvas clipping
# ---------------------------------------------------------------------------


def test_canvas_size_and_symmetric_clipping(sample_root, tmp_path):
    report = run_sample(sample_root, "clip", str(tmp_path))
    assert (report["canvasWidth"], report["canvasHeight"]) == (200, 200)
    preview = load_preview(str(tmp_path))
    assert preview.size == (200, 200)

    big = layer_by_id(report, "big")
    assert big["clipped"] is True
    assert big["size"] == [250, 250]
    assert big["position"] == [-25, -25]
    # Symmetric: equal margins on every edge.
    assert big["bbox"] == [2, 2, 248, 248]        # inside the layer file
    assert big["canvasBBox"] == [0, 0, 200, 200]  # on the canvas
    assert big["contributedPixels"] < big["visiblePixels"]

    # The canvas keeps rendering everywhere: no crash, alpha preserved.
    for xy in [(0, 0), (199, 0), (0, 199), (199, 199), (100, 100)]:
        assert preview.getpixel(xy)[3] > 0


def test_clipping_keeps_running_with_multiple_layers(sample_root, tmp_path):
    report = run_sample(sample_root, "metrics", str(tmp_path))
    corner = layer_by_id(report, "corner")
    assert corner["clipped"] is True
    assert corner["canvasBBox"] == [60, 60, 64, 64]
    assert corner["contributedPixels"] == 16  # 4x4 of the 10x10 layer survives


# ---------------------------------------------------------------------------
# 5. crops
# ---------------------------------------------------------------------------


def test_crops_dir_has_one_png_per_used_layer(sample_root, tmp_path):
    for name in ("order", "metrics"):
        out_dir = str(tmp_path / name)
        report = run_sample(sample_root, name, out_dir)
        crops_dir = os.path.join(out_dir, "crops")
        assert os.path.isdir(crops_dir)
        files = sorted(os.listdir(crops_dir))
        assert len(files) == report["usedLayers"]
        for layer in report["layers"]:
            if layer["status"] != "used":
                continue
            crop = layer["cropFile"]
            assert os.path.basename(crop).startswith(layer["id"])
            with Image.open(os.path.join(out_dir, crop)) as img:
                assert img.mode == "RGBA"
                assert img.getchannel("A") is not None
                assert "A" in img.getbands()


def test_crop_preserves_transparency(sample_root, tmp_path):
    report = run_sample(sample_root, "metrics", str(tmp_path / "metrics"))
    out_dir = str(tmp_path / "metrics")
    with Image.open(os.path.join(out_dir, layer_by_id(report, "empty")["cropFile"])) as img:
        assert img.convert("RGBA").getchannel("A").getbbox() is None
    with Image.open(os.path.join(out_dir, layer_by_id(report, "partial")["cropFile"])) as img:
        alpha = img.convert("RGBA").getchannel("A")
        bbox = alpha.getbbox()
        assert bbox is not None
        # Half of the 8x16 visible area is transparent.
        assert sum(alpha.histogram()[1:]) == 8 * 16


# ---------------------------------------------------------------------------
# 6. per-layer metrics
# ---------------------------------------------------------------------------


def test_required_metric_fields_present(sample_root, tmp_path):
    report = run_sample(sample_root, "metrics", str(tmp_path))
    required = {"bbox", "visiblePixels", "fullyTransparent", "clipped"}
    for layer in report["layers"]:
        assert required <= set(layer)


def test_metric_values_match_sample_pixels(sample_root, tmp_path):
    report = run_sample(sample_root, "metrics", str(tmp_path))

    opaque = layer_by_id(report, "opaque")
    assert opaque["visiblePixels"] == 342  # 400 minus 12% removed noise pixels
    assert opaque["fullyTransparent"] is False

    empty = layer_by_id(report, "empty")
    assert empty["bbox"] is None
    assert empty["visiblePixels"] == 0
    assert empty["fullyTransparent"] is True
    assert empty["clipped"] is False

    partial = layer_by_id(report, "partial")
    assert partial["bbox"] == [8, 0, 16, 16]          # inside the layer file
    assert partial["canvasBBox"] == [38, 0, 46, 16]   # on the canvas
    assert partial["visiblePixels"] == 128


# ---------------------------------------------------------------------------
# 7. missing / undecodable
# ---------------------------------------------------------------------------


def test_missing_layer_is_skipped_and_reported(sample_root, tmp_path):
    report = run_sample(sample_root, "missing", str(tmp_path))
    assert report["unavailableLayers"] == ["two"]
    two = layer_by_id(report, "two")
    assert two["status"] == "unavailable"
    assert two["cropFile"] is None
    preview = load_preview(str(tmp_path))
    # two.png was (240,80,40) at (4,4); its absence must show "one" instead.
    assert preview.getpixel((6, 6)) == (240, 80, 40, 255)
    assert preview.getpixel((28, 28)) == (40, 80, 240, 255)  # "three" still drawn


def test_undecodable_layer_is_skipped_and_reported(sample_root, tmp_path):
    report = run_sample(sample_root, "corrupt", str(tmp_path))
    assert report["unavailableLayers"] == ["broken"]
    assert layer_by_id(report, "broken")["status"] == "unavailable"
    preview = load_preview(str(tmp_path))
    assert preview.getpixel((2, 2)) == (30, 160, 90, 255)
    # (10,10) is "fine" (alpha 200) over "good"; the skipped broken.png would
    # otherwise tint this area, so the hand-computed source-over proves it is
    # absent from the preview.
    expected = layer_compositor.composite_pixel(
        (30, 160, 90, 255), (220, 120, 30, 200), "normal", 100
    )
    assert preview.getpixel((10, 10)) == expected


# ---------------------------------------------------------------------------
# 8/9. duplicates and top-level summary
# ---------------------------------------------------------------------------


def test_duplicate_ids_keep_lowest_order(sample_root, tmp_path):
    report = run_sample(sample_root, "duplicate", str(tmp_path))
    assert report["duplicateIds"] == ["dup"]
    assert report["duplicates"] == [
        {"id": "dup", "keptOrder": 1, "ignoredOrders": [2]}
    ]
    assert report["ignoredDuplicates"] == [{"id": "dup", "order": 2}]
    assert [layer["id"] for layer in report["layers"]] == ["dup", "solo"]
    assert layer_by_id(report, "dup")["order"] == 1

    preview = load_preview(str(tmp_path))
    # The kept dup sits at (0,0) (order=1); the ignored one would be at (4,4)
    # but is indistinguishable there, so check "solo" wins the centre instead.
    assert preview.getpixel((1, 1)) == (200, 50, 50, 255)
    assert preview.getpixel((10, 10)) == (50, 200, 50, 255)


def test_summary_keys_and_values(sample_root, tmp_path):
    expected_totals = {
        "order": (3, 3, [], []),
        "blend": (3, 3, [], []),
        "opacity": (3, 3, [], []),
        "clip": (1, 1, [], []),
        "metrics": (4, 4, [], []),
        "missing": (3, 2, ["two"], []),
        "corrupt": (3, 2, ["broken"], []),
        "duplicate": (3, 2, [], ["dup"]),
    }
    for name, (total, used, unavailable, duplicates) in expected_totals.items():
        report = run_sample(sample_root, name, str(tmp_path / name))
        for key in ("totalLayers", "usedLayers", "unavailableLayers", "duplicateIds"):
            assert key in report
        manifest_path = os.path.join(sample_root, name, "manifest.json")
        with open(manifest_path, encoding="utf-8") as handle:
            manifest = json.load(handle)
        assert report["totalLayers"] == len(manifest["layers"]) == total
        assert report["usedLayers"] == used
        assert report["unavailableLayers"] == unavailable
        assert report["duplicateIds"] == duplicates


def test_rerun_clears_stale_crops(sample_root, tmp_path):
    out_dir = str(tmp_path / "rerun")
    run_sample(sample_root, "metrics", out_dir)
    stale = os.path.join(out_dir, "crops", "leftover.png")
    with open(stale, "wb") as handle:
        handle.write(b"stale")
    report = run_sample(sample_root, "metrics", out_dir)
    assert not os.path.exists(stale)
    assert any("removed stale crop" in note for note in report["warnings"])


def test_fully_off_canvas_layer_still_gets_a_crop(sample_root, tmp_path):
    layers_dir = str(tmp_path / "offcanvas-layers")
    os.makedirs(layers_dir)
    with Image.open(os.path.join(sample_root, "order", "layers", "a.png")) as img:
        img.save(os.path.join(layers_dir, "off.png"), format="PNG")
    manifest = {
        "canvasWidth": 20, "canvasHeight": 20,
        "layers": [{"id": "off", "order": 1, "x": -50, "y": -50}],
    }
    manifest_path = tmp_path / "offcanvas.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    report = layer_compositor.run(str(manifest_path), layers_dir,
                                  str(tmp_path / "offcanvas-out"))
    layer = layer_by_id(report, "off")
    assert layer["clipped"] is True
    assert layer["canvasBBox"] is None
    assert layer["contributedPixels"] == 0
    crop_path = tmp_path / "offcanvas-out" / layer["cropFile"]
    assert crop_path.is_file()
    with Image.open(crop_path) as crop:
        assert crop.mode == "RGBA"


# ---------------------------------------------------------------------------
# CLI + safety
# ---------------------------------------------------------------------------


def test_cli_runs_and_writes_outputs(sample_root, tmp_path):
    out_dir = str(tmp_path / "cli")
    result = subprocess.run(
        [sys.executable, "-I", os.path.join(PROJECT_ROOT, "layer_compositor.py"),
         "--manifest", os.path.join(sample_root, "missing", "manifest.json"),
         "--layers", os.path.join(sample_root, "missing", "layers"),
         "--out", out_dir],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "unavailable=1" in result.stdout
    assert os.path.isfile(os.path.join(out_dir, "preview.png"))
    assert os.path.isfile(os.path.join(out_dir, "report.json"))


def test_path_traversal_in_manifest_is_refused(sample_root, tmp_path):
    manifest = {
        "canvasWidth": 8, "canvasHeight": 8,
        "layers": [{"id": "evil", "order": 1, "file": "../outside.png"}],
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    report = layer_compositor.run(
        str(manifest_path), os.path.join(sample_root, "order", "layers"),
        str(tmp_path / "out"),
    )
    assert report["unavailableLayers"] == ["evil"]
    notes = layer_by_id(report, "evil")["notes"]
    assert any("unsafe" in note for note in notes)


def test_manifest_must_have_valid_canvas(tmp_path):
    manifest_path = tmp_path / "bad.json"
    manifest_path.write_text(
        json.dumps({"canvasWidth": 0, "canvasHeight": "x", "layers": []}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        layer_compositor.run(str(manifest_path), str(tmp_path), str(tmp_path / "out"))
