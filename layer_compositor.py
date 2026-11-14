"""Deterministic 2D layer compositor.

Reads a layer manifest (JSON) plus a directory of transparent PNG layers, stacks
them by ``order`` using per-layer blend modes and global opacity, and writes:

* ``preview.png``  - the composited RGBA preview
* ``crops/<id>.png`` - per-layer transparent crop previews
* ``report.json``  - per-layer audit data for art review

Only Pillow is required. Composite maths is performed in straight-alpha space
using the W3C compositing model, so results match common DCC tools.

Usage::

    python layer_compositor.py --manifest samples/order/manifest.json \\
        --layers samples/order/layers --out out/order
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from dataclasses import dataclass, field
from typing import Any

from PIL import Image, ImageChops

SUPPORTED_BLEND_MODES = ("normal", "multiply")
REPORT_FILENAME = "report.json"
PREVIEW_FILENAME = "preview.png"
CROPS_DIRNAME = "crops"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class LayerSpec:
    """One manifest layer after normalisation."""

    entry: dict[str, Any]
    manifest_index: int
    id: str
    file_name: str
    order: int
    offset_x: int
    offset_y: int
    align: str | None
    opacity_requested: int
    opacity: int
    blend_mode_requested: str | None
    blend_mode: str = "normal"
    blend_mode_fallback: bool = False
    duplicate: bool = False

    @property
    def opacity_clamped(self) -> bool:
        return self.opacity_requested != self.opacity


@dataclass
class LayerResult:
    """Outcome of processing one layer.

    Coordinate conventions used in the report:

    * ``bbox`` - alpha bounding box inside the layer file, ``[l, t, r, b]``
      with the right/bottom edges exclusive; ``None`` when fully transparent.
    * ``canvasBBox`` - the same rectangle in canvas coordinates, clipped to the
      canvas; ``None`` when nothing visible survives on the canvas.
    * ``visiblePixels`` - alpha > 0 pixels in the layer file itself.
    * ``contributedPixels`` - alpha > 0 pixels that actually reach the canvas
      (i.e. inside ``canvasBBox``).
    """

    spec: LayerSpec
    status: str = "used"  # used | unavailable | ignored
    notes: list[str] = field(default_factory=list)
    crop_file: str | None = None
    bbox: list[int] | None = None
    canvas_bbox: list[int] | None = None
    visible_pixels: int = 0
    contributed_pixels: int = 0
    fully_transparent: bool | None = True
    clipped: bool | None = False
    position: list[int] | None = None
    size: list[int] | None = None


# ---------------------------------------------------------------------------
# Manifest normalisation (all manifest values are untrusted)
# ---------------------------------------------------------------------------


def _to_int(value: Any) -> int | None:
    """Best-effort integer conversion; booleans are rejected on purpose."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    number: float | None = None
    if isinstance(value, float):
        number = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return int(text)
        except ValueError:
            pass
        try:
            number = float(text)
        except ValueError:
            return None
    if number is None or not math.isfinite(number):
        return None
    return int(round(number))


def _parse_id(value: Any) -> str | None:
    if value is None or isinstance(value, (bool, dict, list)):
        return None
    text = str(value).strip()
    return text or None


def _parse_file_name(value: Any) -> str | None:
    """Only a plain file name is accepted; directory traversal is refused."""
    if value is None:
        return None
    name = str(value).strip()
    if not name:
        return None
    if os.path.basename(name) != name or name in (".", ".."):
        return None
    return name


def clamp_opacity(value: int) -> int:
    """Clamp a global layer opacity to the inclusive 0..100 range."""
    return max(0, min(100, value))


def _safe_align(value: Any) -> str | None:
    if value is None:
        return None
    align = str(value).strip().lower()
    tokens = align.split()
    allowed = {"left", "right", "center", "top", "bottom"}
    if not tokens or any(token not in allowed for token in tokens) or len(tokens) > 2:
        return None
    return " ".join(tokens)


def load_manifest(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8-sig") as handle:
        manifest = json.load(handle)
    if not isinstance(manifest, dict):
        raise ValueError("manifest root must be a JSON object")
    return manifest


def canvas_size(manifest: dict[str, Any]) -> tuple[int, int]:
    width = _to_int(manifest.get("canvasWidth"))
    height = _to_int(manifest.get("canvasHeight"))
    if width is None or height is None or width <= 0 or height <= 0:
        raise ValueError("canvasWidth/canvasHeight must be positive integers")
    return width, height


def parse_layers(manifest: dict[str, Any], warnings: list[str]) -> list[LayerSpec]:
    raw_layers = manifest.get("layers")
    if not isinstance(raw_layers, list):
        raise ValueError("manifest must contain a 'layers' array")

    specs: list[LayerSpec] = []
    for index, entry in enumerate(raw_layers):
        if not isinstance(entry, dict):
            warnings.append(f"layers[{index}] is not an object; skipped")
            continue

        layer_id = _parse_id(entry.get("id"))
        if layer_id is None:
            warnings.append(f"layers[{index}] has no usable id; skipped")
            continue

        raw_order = _to_int(entry.get("order"))
        if raw_order is None:
            warnings.append(f"layer {layer_id!r}: missing/invalid order, using 0")
            raw_order = 0

        opacity_requested = _to_int(entry.get("opacity", 100))
        if opacity_requested is None:
            warnings.append(f"layer {layer_id!r}: invalid opacity, using 100")
            opacity_requested = 100

        raw_mode = entry.get("blendMode")
        blend_mode_requested = None if raw_mode is None else str(raw_mode).strip()

        # An unsafe file name is passed through unchanged so the loader (which
        # re-validates against the layer directory) can report it explicitly
        # rather than silently substituting a different file.
        file_name = _parse_file_name(entry.get("file"))
        if file_name is None:
            raw_file = entry.get("file")
            if raw_file is not None and str(raw_file).strip():
                file_name = str(raw_file).strip()
            else:
                file_name = f"{layer_id}.png"
        align = _safe_align(entry.get("align"))
        if entry.get("align") is not None and align is None:
            warnings.append(f"layer {layer_id!r}: unsupported align, ignored")

        spec = LayerSpec(
            entry=entry,
            manifest_index=index,
            id=layer_id,
            file_name=file_name,
            order=raw_order,
            offset_x=_to_int(entry.get("x")) or 0,
            offset_y=_to_int(entry.get("y")) or 0,
            align=align,
            opacity_requested=opacity_requested,
            opacity=clamp_opacity(opacity_requested),
            blend_mode_requested=blend_mode_requested,
        )
        mode = (blend_mode_requested or "").lower()
        if mode in SUPPORTED_BLEND_MODES:
            spec.blend_mode = mode
        else:
            spec.blend_mode = "normal"
            spec.blend_mode_fallback = True
        specs.append(spec)
    return specs


def resolve_duplicates(specs: list[LayerSpec]) -> list[dict[str, Any]]:
    """Mark duplicate-id entries, keep only the lowest order; return summary."""
    winners: dict[str, LayerSpec] = {}
    for spec in specs:
        current = winners.get(spec.id)
        if current is None or spec.order < current.order:
            winners[spec.id] = spec

    ignored_orders: dict[str, list[int]] = {}
    for spec in specs:
        if winners[spec.id] is not spec:
            spec.duplicate = True
            ignored_orders.setdefault(spec.id, []).append(spec.order)

    groups = []
    for layer_id, orders in sorted(
        ignored_orders.items(),
        key=lambda item: (min(item[1]), winners[item[0]].manifest_index),
    ):
        groups.append(
            {
                "id": layer_id,
                "keptOrder": winners[layer_id].order,
                "ignoredOrders": sorted(orders),
            }
        )
    return groups


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------


def _align_offset(align: str, canvas: tuple[int, int],
                  size: tuple[int, int]) -> tuple[int, int]:
    cw, ch = canvas
    w, h = size
    dx = dy = 0
    tokens = align.split()
    if len(tokens) == 1 and tokens[0] == "center":
        return math.floor((cw - w) / 2.0 + 0.5), math.floor((ch - h) / 2.0 + 0.5)
    for token in tokens:
        if token == "left":
            dx = 0
        elif token == "right":
            dx = cw - w
        elif token == "center":
            dx = math.floor((cw - w) / 2.0 + 0.5)
        elif token == "top":
            dy = 0
        elif token == "bottom":
            dy = ch - h
    return dx, dy


def layer_position(spec: LayerSpec, canvas: tuple[int, int],
                   size: tuple[int, int]) -> tuple[int, int]:
    if spec.align:
        dx, dy = _align_offset(spec.align, canvas, size)
        return dx + spec.offset_x, dy + spec.offset_y
    return spec.offset_x, spec.offset_y


def _intersect(a: list[int], b: list[int]) -> list[int] | None:
    left, top = max(a[0], b[0]), max(a[1], b[1])
    right, bottom = min(a[2], b[2]), min(a[3], b[3])
    if right <= left or bottom <= top:
        return None
    return [left, top, right, bottom]


def _translate(bbox: list[int], offset: tuple[int, int]) -> list[int]:
    return [bbox[0] + offset[0], bbox[1] + offset[1],
            bbox[2] + offset[0], bbox[3] + offset[1]]


# ---------------------------------------------------------------------------
# Image maths (straight alpha, W3C compositing model)
# ---------------------------------------------------------------------------


def _blend_channel(base: int, top: int, mode: str) -> int:
    if mode == "multiply":
        return (base * top + 127) // 255
    return top


def composite_pixel(base: tuple[int, int, int, int],
                    top: tuple[int, int, int, int],
                    mode: str, opacity: int) -> tuple[int, int, int, int]:
    """Reference source-over of one pixel with an optional blend mode.

    ``opacity`` is an integer percentage in 0..100.  Straight-alpha in and out.
    Kept as an independent, readable reference for the vectorised
    :func:`composite_image`; it applies the same 8-bit alpha scaling, so both
    paths agree to within one unit per channel.
    """
    br, bg, bb, ba = base
    tr, tg, tb, ta = top
    if opacity <= 0 or ta == 0:
        return base
    scaled_alpha = max(0, min(255, int(ta * (opacity / 100.0) + 0.5)))
    source_a = scaled_alpha / 255.0
    base_a = ba / 255.0
    out_a = source_a + base_a * (1.0 - source_a)
    if out_a <= 0.0:
        return (0, 0, 0, 0)

    channels = []
    for base_c, top_c in ((br, tr), (bg, tg), (bb, tb)):
        blended = _blend_channel(base_c, top_c, mode)
        premultiplied = (
            source_a * (1.0 - base_a) * top_c
            + source_a * base_a * blended
            + (1.0 - source_a) * base_a * base_c
        )
        value = premultiplied / out_a
        channels.append(max(0, min(255, int(value + 0.5))))
    alpha = max(0, min(255, int(out_a * 255.0 + 0.5)))
    if alpha == 0:
        # Canonical transparent pixel (matches _clear_transparent_rgb).
        return (0, 0, 0, 0)
    return (channels[0], channels[1], channels[2], alpha)


def _scaled_opacity(top: Image.Image, opacity: int) -> Image.Image:
    """Apply a global opacity percentage to an image's alpha channel."""
    if opacity >= 100:
        return top
    top_r, top_g, top_b, top_a = top.split()
    alpha = top_a.point(lambda value: int(value * (opacity / 100.0) + 0.5))
    return Image.merge("RGBA", (top_r, top_g, top_b, alpha))


def _clear_transparent_rgb(image: Image.Image) -> Image.Image:
    """Zero out colour in fully transparent pixels so output is canonical.

    PNG stores straight alpha, so the RGB of a pixel with alpha 0 is invisible
    but would otherwise depend on the compositing path; clearing it keeps the
    preview byte-for-byte reproducible.
    """
    alpha = image.getchannel("A")
    opaque_mask = alpha.point(lambda value: 255 if value > 0 else 0)
    rgb = Image.new("RGB", image.size, (0, 0, 0))
    rgb.paste(image.convert("RGB"), (0, 0), opaque_mask)
    return Image.merge("RGBA", (*rgb.split(), alpha))


def composite_image(base: Image.Image, top: Image.Image, mode: str,
                    opacity: int) -> Image.Image:
    """Composite two equally sized RGBA images, returning a new RGBA image.

    Uses Pillow's optimised C paths for the normal case and for the blend
    channel maths, so the per-pixel Python loop is only a reference/fallback.
    """
    if opacity <= 0:
        return base.copy()
    if top.size != base.size:
        raise ValueError("composite_image requires equally sized images")

    source = _scaled_opacity(top, opacity)
    if mode == "normal":
        return _clear_transparent_rgb(Image.alpha_composite(base, source))

    if mode != "multiply":
        raise ValueError(f"unsupported blend mode: {mode!r}")

    base_alpha = base.getchannel("A")
    if base_alpha.getbbox() is None:
        # Nothing underneath anywhere: blending is irrelevant, the source wins.
        return _clear_transparent_rgb(source)

    # W3C blend formula: co = as(1-ab)Cs + as·ab·B(Cb, Cs) + (1-as)ab·Cb.
    # Blend against the base colour only where the base has coverage, then
    # source-over with the source alpha.
    base_rgb = base.convert("RGB")
    source_rgb = source.convert("RGB")
    blended = ImageChops.multiply(base_rgb, source_rgb)
    blended_rgba = Image.merge(
        "RGBA",
        (*Image.composite(blended, source_rgb, base_alpha).split(),
         source.getchannel("A")),
    )
    return _clear_transparent_rgb(Image.alpha_composite(base, blended_rgba))


def _alpha_histogram_count(image: Image.Image) -> int:
    """Number of pixels with alpha > 0."""
    return sum(image.getchannel("A").histogram()[1:])


# ---------------------------------------------------------------------------
# Compositing pipeline
# ---------------------------------------------------------------------------


def _resolve_layer_path(layer_dir: str, file_name: str) -> str | None:
    candidate = os.path.realpath(os.path.join(layer_dir, file_name))
    root = os.path.realpath(layer_dir)
    try:
        if os.path.commonpath([candidate, root]) != root:
            return None
    except ValueError:
        return None
    return candidate


def load_layer_image(layer_dir: str, file_name: str) -> tuple[Image.Image | None, str | None]:
    path = _resolve_layer_path(layer_dir, file_name)
    if path is None:
        return None, f"refused unsafe file name: {file_name!r}"
    if not os.path.isfile(path):
        return None, f"layer file not found: {file_name}"
    try:
        with Image.open(path) as raw:
            raw.load()
            return raw.convert("RGBA"), None
    except Exception as exc:  # noqa: BLE001 - any decode problem marks the layer unavailable
        return None, f"failed to decode {file_name}: {exc}"


def _save_transparent_crop(source: Image.Image, canvas_bbox: list[int] | None,
                           out_path: str) -> None:
    """Save the on-canvas visible part; a 1x1 transparent PNG if there is none."""
    if canvas_bbox is None:
        Image.new("RGBA", (1, 1), (0, 0, 0, 0)).save(out_path, format="PNG")
        return
    source.crop(tuple(canvas_bbox)).save(out_path, format="PNG")


def _crop_name(layer_id: str, taken: set[str]) -> str:
    """File name for a crop: a filesystem-safe form of the layer id.

    Every character that Windows forbids in a name is replaced with ``_`` (so
    ``a/b`` becomes ``a_b`` and still starts with the id), and the name is
    de-duplicated inside the run if sanitisation collapses two ids together.
    """
    safe = "".join(
        ch if (ch.isalnum() or ch in "-_. ()[]") else "_" for ch in layer_id
    ).rstrip(" .")[:116]
    if not safe:
        safe = "layer"
    name = safe + ".png"
    suffix = 2
    while name.lower() in taken:
        name = f"{safe}-{suffix}.png"
        suffix += 1
    taken.add(name.lower())
    return name


def build_layer_report(result: LayerResult) -> dict[str, Any]:
    """Fixed report schema; keys are part of the tool's contract."""
    spec = result.spec
    return {
        "id": spec.id,
        "order": spec.order,
        "manifestIndex": spec.manifest_index,
        "status": result.status,
        "file": spec.file_name,
        "blendMode": spec.blend_mode,
        "blendModeRequested": spec.blend_mode_requested,
        "blendModeFallback": spec.blend_mode_fallback,
        "opacity": spec.opacity,
        "opacityRequested": spec.opacity_requested,
        "opacityClamped": spec.opacity_clamped,
        "position": result.position,
        "size": result.size,
        "bbox": result.bbox,
        "canvasBBox": result.canvas_bbox,
        "visiblePixels": result.visible_pixels,
        "contributedPixels": result.contributed_pixels,
        "fullyTransparent": result.fully_transparent,
        "clipped": result.clipped,
        "cropFile": result.crop_file,
        "notes": result.notes,
    }


def run(manifest_path: str, layers_dir: str, out_dir: str) -> dict[str, Any]:
    warnings: list[str] = []
    manifest = load_manifest(manifest_path)
    canvas_width, canvas_height = canvas_size(manifest)
    canvas = (canvas_width, canvas_height)
    raw_layers = manifest.get("layers")
    if not isinstance(raw_layers, list):
        raise ValueError("manifest must contain a 'layers' array")
    specs = parse_layers(manifest, warnings)
    duplicate_groups = resolve_duplicates(specs)

    os.makedirs(out_dir, exist_ok=True)
    crops_dir = os.path.join(out_dir, CROPS_DIRNAME)
    os.makedirs(crops_dir, exist_ok=True)

    ordered = sorted(specs, key=lambda spec: (spec.order, spec.manifest_index))
    composite = Image.new("RGBA", canvas, (0, 0, 0, 0))
    results: list[LayerResult] = []
    crop_names: set[str] = set()

    for spec in ordered:
        result = LayerResult(spec=spec)
        if spec.duplicate:
            result.status = "ignored"
            group = next(group for group in duplicate_groups if group["id"] == spec.id)
            result.notes.append(
                f"duplicate id {spec.id!r}: order={spec.order} ignored "
                f"(kept order={group['keptOrder']})"
            )
            results.append(result)
            continue

        if spec.opacity_clamped:
            result.notes.append(
                f"opacity clamped {spec.opacity_requested} -> {spec.opacity}"
            )
        if spec.blend_mode_fallback:
            requested = spec.blend_mode_requested
            if requested is None:
                result.notes.append("blendMode missing; using normal")
            else:
                result.notes.append(
                    f"unsupported blendMode {requested!r}; using normal"
                )
        if spec.align is None and (spec.entry.get("align") is not None):
            result.notes.append("unsupported align value ignored")

        image, error = load_layer_image(layers_dir, spec.file_name)
        if image is None:
            result.status = "unavailable"
            result.fully_transparent = None
            result.clipped = None
            result.notes.append(error or "layer unavailable")
            results.append(result)
            continue

        size = image.size
        position = layer_position(spec, canvas, size)
        placed = Image.new("RGBA", canvas, (0, 0, 0, 0))
        placed.paste(image, position)

        source_bbox = image.getchannel("A").getbbox()
        result.bbox = list(source_bbox) if source_bbox else None
        result.size = [size[0], size[1]]
        result.position = [position[0], position[1]]
        result.visible_pixels = _alpha_histogram_count(image)
        result.fully_transparent = result.visible_pixels == 0
        result.clipped = (
            position[0] < 0
            or position[1] < 0
            or position[0] + size[0] > canvas_width
            or position[1] + size[1] > canvas_height
        )

        canvas_bbox = None
        if source_bbox:
            canvas_bbox = _intersect(
                _translate(list(source_bbox), position),
                [0, 0, canvas_width, canvas_height],
            )
        result.canvas_bbox = canvas_bbox
        if canvas_bbox and spec.opacity > 0:
            result.contributed_pixels = _alpha_histogram_count(
                placed.crop(tuple(canvas_bbox))
            )
        else:
            result.contributed_pixels = 0

        if not result.fully_transparent and canvas_bbox is None:
            result.notes.append("layer lies fully outside the canvas; "
                                "nothing of it appears in the preview")
        if result.fully_transparent:
            result.notes.append("layer is fully transparent")
        elif spec.opacity == 0:
            result.notes.append("opacity is 0; layer is invisible in the preview")

        crop_name = _crop_name(spec.id, crop_names)
        _save_transparent_crop(placed, canvas_bbox, os.path.join(crops_dir, crop_name))
        result.crop_file = f"{CROPS_DIRNAME}/{crop_name}"

        composite = composite_image(composite, placed, spec.blend_mode, spec.opacity)
        results.append(result)

    preview_path = os.path.join(out_dir, PREVIEW_FILENAME)
    composite.save(preview_path, format="PNG")

    # Keep the crops directory in sync with this run so "one PNG per used
    # layer" also holds when re-running into an existing output directory.
    for existing in os.listdir(crops_dir):
        if existing.lower().endswith(".png") and existing.lower() not in crop_names:
            try:
                os.remove(os.path.join(crops_dir, existing))
                warnings.append(f"removed stale crop: {CROPS_DIRNAME}/{existing}")
            except OSError as exc:
                warnings.append(f"could not remove stale crop {existing}: {exc}")

    used = [result for result in results if result.status == "used"]
    unavailable = [result for result in results if result.status == "unavailable"]
    ignored = [result for result in results if result.status == "ignored"]

    report = {
        "schemaVersion": 1,
        "canvasWidth": canvas_width,
        "canvasHeight": canvas_height,
        "previewFile": PREVIEW_FILENAME,
        "cropsDir": CROPS_DIRNAME,
        "totalLayers": len(raw_layers),
        "usedLayers": len(used),
        "unavailableLayers": [result.spec.id for result in unavailable],
        "duplicateIds": [group["id"] for group in duplicate_groups],
        "duplicates": duplicate_groups,
        "ignoredDuplicates": [
            {"id": result.spec.id, "order": result.spec.order}
            for result in ignored
        ],
        "invalidEntries": len(raw_layers) - len(specs),
        "warnings": warnings,
        # One entry per distinct layer id, in compositing order.  Entries that
        # lost a duplicate-id conflict are summarised under ignoredDuplicates
        # instead, so this list stays aligned with the preview.
        "layers": [build_layer_report(result) for result in results
                   if result.status != "ignored"],
    }

    with open(os.path.join(out_dir, REPORT_FILENAME), "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Composite transparent PNG layers according to a JSON manifest."
    )
    parser.add_argument("--manifest", required=True, help="path to manifest JSON")
    parser.add_argument("--layers", required=True, help="directory containing PNG layers")
    parser.add_argument("--out", required=True, help="output directory")
    args = parser.parse_args(argv)

    try:
        report = run(args.manifest, args.layers, args.out)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(
        f"layers: total={report['totalLayers']} used={report['usedLayers']} "
        f"unavailable={len(report['unavailableLayers'])} "
        f"duplicates={len(report['duplicateIds'])}"
    )
    print(f"preview: {os.path.join(args.out, report['previewFile'])}")
    print(f"report:  {os.path.join(args.out, REPORT_FILENAME)}")
    if report["unavailableLayers"]:
        print("unavailable: " + ", ".join(report["unavailableLayers"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

