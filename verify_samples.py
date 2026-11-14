"""Run every local sample and print a human-readable acceptance report.

    python verify_samples.py

It regenerates the samples (deterministically), composites each one into
``out/<sample>/`` and checks the acceptance criteria from the task statement,
printing PASS/FAIL per item.  Exit code is non-zero if anything fails.
"""

from __future__ import annotations

import json
import os
import sys

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import layer_compositor  # noqa: E402
import samples.generate_samples as sample_gen  # noqa: E402

OUT_ROOT = os.path.join(HERE, "out")
checks: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    checks.append((name, bool(ok), detail))


def run(name: str) -> tuple[dict, str]:
    out_dir = os.path.join(OUT_ROOT, name)
    report = layer_compositor.run(
        os.path.join(HERE, "samples", name, "manifest.json"),
        os.path.join(HERE, "samples", name, "layers"),
        out_dir,
    )
    return report, out_dir


def preview_pixel(out_dir: str, xy: tuple[int, int]) -> tuple[int, ...]:
    with Image.open(os.path.join(out_dir, "preview.png")) as img:
        return img.convert("RGBA").getpixel(xy)


def by_id(report: dict, layer_id: str) -> dict:
    return next(layer for layer in report["layers"] if layer["id"] == layer_id)


def main() -> int:
    sample_gen.main([])
    os.makedirs(OUT_ROOT, exist_ok=True)

    # --- 1. stacking order -------------------------------------------------
    report, out_dir = run("order")
    check("1. report order follows manifest order (b, a, c)",
          [layer["id"] for layer in report["layers"]] == ["b", "a", "c"],
          str([layer["id"] for layer in report["layers"]]))
    a_over_b = preview_pixel(out_dir, (22, 22)) == (220, 40, 40, 255)
    c_over_a = preview_pixel(out_dir, (40, 40)) == (240, 200, 30, 255)
    check("1. preview: a covers b and c covers a", a_over_b and c_over_a,
          f"a@(22,22)={preview_pixel(out_dir, (22, 22))}, "
          f"c@(40,40)={preview_pixel(out_dir, (40, 40))}")

    # --- 2. blend modes ----------------------------------------------------
    report, out_dir = run("blend")
    expected_mult = tuple((b * t + 127) // 255
                          for b, t in zip((200, 120, 60), (128, 255, 64)))
    got = preview_pixel(out_dir, (5, 5))
    check("2. multiply pixel matches hand calculation",
          got == (*expected_mult, 255),
          f"got {got}, expected {(*expected_mult, 255)}")
    unknown = by_id(report, "unknown")
    check("2. unknown mode falls back to normal and is reported",
          unknown["blendMode"] == "normal" and unknown["blendModeFallback"],
          f"blendMode={unknown['blendMode']}, "
          f"requested={unknown['blendModeRequested']!r}")
    check("2. unknown-mode layer composites as normal",
          preview_pixel(out_dir, (25, 5)) == (10, 200, 120, 255),
          str(preview_pixel(out_dir, (25, 5))))

    # --- 3. opacity clamping ----------------------------------------------
    report, out_dir = run("opacity")
    high, low = by_id(report, "high"), by_id(report, "low")
    check("3. 150 clamped to 100 and reported",
          high["opacityRequested"] == 150 and high["opacity"] == 100
          and high["opacityClamped"]
          and any("150" in note and "100" in note for note in high["notes"]),
          f"{high['opacityRequested']} -> {high['opacity']}, notes={high['notes']}")
    check("3. -10 clamped to 0 and layer invisible",
          low["opacityRequested"] == -10 and low["opacity"] == 0
          and low["contributedPixels"] == 0,
          f"{low['opacityRequested']} -> {low['opacity']}, "
          f"contributed={low['contributedPixels']}")

    # --- 4. canvas size + clipping ----------------------------------------
    report, out_dir = run("clip")
    big = by_id(report, "big")
    dims_ok = (report["canvasWidth"], report["canvasHeight"]) == (200, 200)
    with Image.open(os.path.join(out_dir, "preview.png")) as img:
        dims_ok = dims_ok and img.size == (200, 200)
    check("4. 200x200 canvas kept for a 250x250 centred layer", dims_ok,
          f"canvas={report['canvasWidth']}x{report['canvasHeight']}, "
          f"layer={big['size']}, position={big['position']}")
    symmetric = (big["canvasBBox"] == [0, 0, 200, 200]
                 and big["position"] == [-25, -25])
    check("4. all four edges cropped symmetrically", symmetric,
          f"canvasBBox={big['canvasBBox']}, position={big['position']}, "
          f"clipped={big['clipped']}")

    # --- 5. crops ----------------------------------------------------------
    report, out_dir = run("metrics")
    crops_dir = os.path.join(out_dir, "crops")
    files = sorted(os.listdir(crops_dir))
    used = [layer for layer in report["layers"] if layer["status"] == "used"]
    names_ok = all(
        os.path.basename(layer["cropFile"]).startswith(layer["id"])
        for layer in used
    )
    alpha_ok = all(
        "A" in Image.open(os.path.join(out_dir, layer["cropFile"])).getbands()
        for layer in used
    )
    check("5. one RGBA crop per used layer, named by id",
          len(files) == len(used) and names_ok and alpha_ok, ", ".join(files))

    # --- 6. per-layer metrics ---------------------------------------------
    opaque, empty, partial = (by_id(report, "opaque"), by_id(report, "empty"),
                              by_id(report, "partial"))
    fields_present = all(
        {"bbox", "visiblePixels", "fullyTransparent", "clipped"} <= set(layer)
        for layer in report["layers"]
    )
    check("6. all four metric fields present on every layer", fields_present)
    check("6. metric values match sample pixels",
          opaque["visiblePixels"] == 342 and empty["visiblePixels"] == 0
          and empty["fullyTransparent"] is True and empty["bbox"] is None
          and partial["visiblePixels"] == 128
          and partial["canvasBBox"] == [38, 0, 46, 16],
          f"opaque={opaque['visiblePixels']}, empty={empty['visiblePixels']}, "
          f"partial={partial['visiblePixels']}")

    # --- 7. missing / undecodable -----------------------------------------
    report, out_dir = run("missing")
    check("7. missing layer marked unavailable, others composited",
          report["unavailableLayers"] == ["two"]
          and by_id(report, "two")["status"] == "unavailable"
          and preview_pixel(out_dir, (6, 6)) == (240, 80, 40, 255)
          and preview_pixel(out_dir, (28, 28)) == (40, 80, 240, 255),
          f"unavailable={report['unavailableLayers']}")
    report, out_dir = run("corrupt")
    check("7. undecodable PNG marked unavailable",
          report["unavailableLayers"] == ["broken"]
          and preview_pixel(out_dir, (2, 2)) == (30, 160, 90, 255),
          f"unavailable={report['unavailableLayers']}")

    # --- 8. duplicates -----------------------------------------------------
    report, out_dir = run("duplicate")
    kept = by_id(report, "dup")
    check("8. duplicate id: only order=1 used, order=2 ignored and listed",
          report["duplicateIds"] == ["dup"]
          and report["duplicates"] == [
              {"id": "dup", "keptOrder": 1, "ignoredOrders": [2]}]
          and kept["order"] == 1
          and preview_pixel(out_dir, (1, 1)) == (200, 50, 50, 255),
          f"duplicates={report['duplicates']}")

    # --- 9. summary keys ---------------------------------------------------
    report, out_dir = run("duplicate")
    manifest = json.load(open(os.path.join(HERE, "samples", "duplicate",
                                          "manifest.json"), encoding="utf-8"))
    check("9. summary keys totalLayers/usedLayers/unavailableLayers/duplicateIds",
          report["totalLayers"] == len(manifest["layers"]) == 3
          and report["usedLayers"] == 2
          and report["duplicateIds"] == ["dup"],
          f"total={report['totalLayers']}, used={report['usedLayers']}, "
          f"unavailable={report['unavailableLayers']}, "
          f"duplicateIds={report['duplicateIds']}")
    report, out_dir = run("missing")
    check("9. unavailableLayers lists every unavailable id",
          report["unavailableLayers"] == ["two"]
          and report["totalLayers"] == 3 and report["usedLayers"] == 2,
          f"total={report['totalLayers']}, used={report['usedLayers']}, "
          f"unavailable={report['unavailableLayers']}")

    # --- print -------------------------------------------------------------
    width = max(len(name) for name, _, _ in checks)
    for name, ok, detail in checks:
        status = "PASS" if ok else "FAIL"
        line = f"[{status}] {name.ljust(width)}"
        if detail:
            line += f"  ({detail})"
        print(line)

    failed = [name for name, ok, _ in checks if not ok]
    print()
    print(f"{len(checks) - len(failed)}/{len(checks)} checks passed; "
          f"outputs in {OUT_ROOT}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
