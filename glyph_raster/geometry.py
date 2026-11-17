from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable

import numpy as np

from .model import ALGORITHM_VERSION, Contour, Scene, sha256_value

FLATTEN_TOLERANCE_SAMPLES = 0.25
MAX_SUBDIVISION_DEPTH = 14
SCHEMA_TILE_DEPS = "glyph-raster-tile-deps/v1"


@dataclass(frozen=True)
class Placement:
    glyph_id: str
    pen_x: float
    scale: float


@dataclass
class GlyphGeometry:
    glyph_id: str
    glyph_index: int
    pen_x: float
    scale: float
    polygons: list[np.ndarray]
    edges: np.ndarray
    bbox: tuple[float, float, float, float]
    tile_indices: tuple[int, ...]
    outline_sha256: str

    @property
    def empty(self) -> bool:
        return not self.polygons or self.edges.size == 0


@dataclass
class TileSpec:
    index: int
    tx: int
    ty: int
    x0: int
    y0: int
    width: int
    height: int
    dependency: dict
    version: str
    glyph_ids: tuple[str, ...]

    @property
    def key(self) -> str:
        return tile_key(self.tx, self.ty)

    @property
    def sample_bytes(self) -> int:
        return self.width * self.height


def tile_key(tx: int, ty: int) -> str:
    return f"tile_{tx:05d}_{ty:05d}"


def flatten_tolerance(scene: Scene) -> float:
    return FLATTEN_TOLERANCE_SAMPLES / float(scene.supersampling)


def layout_glyphs(scene: Scene) -> dict[str, Placement]:
    pen_x = scene.margin_x
    placements: dict[str, Placement] = {}
    for glyph in scene.glyphs:
        scale = glyph.size / scene.units_per_em
        placements[glyph.glyph_id] = Placement(glyph.glyph_id, pen_x, scale)
        pen_x += glyph.advance * scale + scene.spacing
    return placements


def transform_point(
    scene: Scene, placement: Placement, glyph_x_offset: float, point: tuple[float, float]
) -> tuple[float, float]:
    x_font, y_font = point
    return (
        placement.pen_x + x_font * placement.scale + glyph_x_offset,
        scene.baseline_px - y_font * placement.scale,
    )


def _distance_to_chord(
    control: tuple[float, float],
    start: tuple[float, float],
    end: tuple[float, float],
) -> float:
    px, py = control
    x1, y1 = start
    x2, y2 = end
    dx = x2 - x1
    dy = y2 - y1
    length = math.hypot(dx, dy)
    if length == 0.0:
        return math.hypot(px - x1, py - y1)
    return abs(dy * px - dx * py + x2 * y1 - y2 * x1) / length


def flatten_quadratic(
    start: tuple[float, float],
    control: tuple[float, float],
    end: tuple[float, float],
    tolerance: float,
    *,
    max_depth: int = MAX_SUBDIVISION_DEPTH,
) -> list[tuple[float, float]]:
    """Return points after start, ending with end.

    Subdivision is width-first via deterministic recursion. At the depth limit
    the remaining curve is represented by its chord.
    """

    if tolerance < 0 or not math.isfinite(tolerance):
        raise ValueError("tolerance must be a finite non-negative number")
    if max_depth < 0:
        raise ValueError("max_depth must be non-negative")

    output: list[tuple[float, float]] = []

    def visit(
        p0: tuple[float, float],
        p1: tuple[float, float],
        p2: tuple[float, float],
        depth: int,
    ) -> None:
        if depth >= max_depth or _distance_to_chord(p1, p0, p2) <= tolerance:
            output.append(p2)
            return
        p01 = ((p0[0] + p1[0]) * 0.5, (p0[1] + p1[1]) * 0.5)
        p12 = ((p1[0] + p2[0]) * 0.5, (p1[1] + p2[1]) * 0.5)
        midpoint = ((p01[0] + p12[0]) * 0.5, (p01[1] + p12[1]) * 0.5)
        visit(p0, p01, midpoint, depth + 1)
        visit(midpoint, p12, p2, depth + 1)

    visit(start, control, end, 0)
    return output


def _flatten_contour(
    scene: Scene,
    glyph: object,
    placement: Placement,
    contour: Contour,
    tolerance: float,
) -> list[tuple[float, float]]:
    points = glyph.points  # type: ignore[attr-defined]
    x_offset = glyph.x_offset  # type: ignore[attr-defined]

    def transformed(index: int) -> tuple[float, float]:
        return transform_point(scene, placement, x_offset, points[index])

    start = transformed(contour.start)
    vertices = [start]
    current = start
    for segment in contour.segments:
        endpoint = transformed(segment.to)
        if segment.kind == "line":
            vertices.append(endpoint)
        else:
            assert segment.control is not None
            control = transformed(segment.control)
            vertices.extend(
                flatten_quadratic(
                    current,
                    control,
                    endpoint,
                    tolerance,
                    max_depth=MAX_SUBDIVISION_DEPTH,
                )
            )
        current = endpoint
    if len(vertices) > 1 and vertices[-1] == vertices[0]:
        vertices.pop()
    return vertices


def _polygon_edges(polygons: Iterable[np.ndarray]) -> np.ndarray:
    chunks: list[np.ndarray] = []
    for polygon in polygons:
        if len(polygon) < 2:
            continue
        start = polygon
        end = np.roll(polygon, -1, axis=0)
        keep = start[:, 1] != end[:, 1]
        if not np.any(keep):
            continue
        start = start[keep]
        end = end[keep]
        directions = np.where(end[:, 1] > start[:, 1], 1.0, -1.0)
        chunks.append(
            np.column_stack(
                (start[:, 0], start[:, 1], end[:, 0], end[:, 1], directions)
            )
        )
    if not chunks:
        return np.empty((0, 5), dtype=float)
    return np.concatenate(chunks, axis=0)


def _tile_ranges_for_bbox(scene: Scene, bbox: tuple[float, float, float, float]) -> list[int]:
    xmin, ymin, xmax, ymax = bbox
    xmin = max(0.0, xmin)
    ymin = max(0.0, ymin)
    xmax = min(float(scene.width), xmax)
    ymax = min(float(scene.height), ymax)
    if xmax <= xmin or ymax <= ymin or xmax <= 0 or ymax <= 0:
        return []
    if xmin >= scene.width or ymin >= scene.height:
        return []
    tx0 = max(0, min(scene.tile_columns - 1, math.floor(xmin / scene.tile_width)))
    ty0 = max(0, min(scene.tile_rows - 1, math.floor(ymin / scene.tile_height)))
    tx1 = max(
        0,
        min(
            scene.tile_columns - 1,
            math.ceil(xmax / scene.tile_width) - 1,
        ),
    )
    ty1 = max(
        0,
        min(
            scene.tile_rows - 1,
            math.ceil(ymax / scene.tile_height) - 1,
        ),
    )
    result: list[int] = []
    for ty in range(ty0, ty1 + 1):
        for tx in range(tx0, tx1 + 1):
            result.append(ty * scene.tile_columns + tx)
    return result


def build_glyph_geometries(scene: Scene) -> list[GlyphGeometry]:
    placements = layout_glyphs(scene)
    tolerance = flatten_tolerance(scene)
    result: list[GlyphGeometry] = []
    for glyph_index, glyph in enumerate(scene.glyphs):
        placement = placements[glyph.glyph_id]
        polygons = [
            np.asarray(
                _flatten_contour(scene, glyph, placement, contour, tolerance),
                dtype=float,
            )
            for contour in glyph.contours
        ]
        referenced: set[int] = set()
        for contour in glyph.contours:
            referenced.add(contour.start)
            for segment in contour.segments:
                referenced.add(segment.to)
                if segment.control is not None:
                    referenced.add(segment.control)
        transformed_points = [
            transform_point(scene, placement, glyph.x_offset, glyph.points[index])
            for index in sorted(referenced)
        ]
        xmin = min(point[0] for point in transformed_points)
        ymin = min(point[1] for point in transformed_points)
        xmax = max(point[0] for point in transformed_points)
        ymax = max(point[1] for point in transformed_points)
        bbox = (xmin, ymin, xmax, ymax)
        outline_sha256 = sha256_value(
            {
                "points": [[x, y] for x, y in glyph.points],
                "contours": [contour.as_dict() for contour in glyph.contours],
            }
        )
        result.append(
            GlyphGeometry(
                glyph_id=glyph.glyph_id,
                glyph_index=glyph_index,
                pen_x=placement.pen_x,
                scale=placement.scale,
                polygons=polygons,
                edges=_polygon_edges(polygons),
                bbox=bbox,
                tile_indices=tuple(_tile_ranges_for_bbox(scene, bbox)),
                outline_sha256=outline_sha256,
            )
        )
    return result


def _glyph_dependency(scene: Scene, glyph: object, geometry: GlyphGeometry) -> dict:
    return {
        "id": geometry.glyph_id,
        "outlineSha256": geometry.outline_sha256,
        "advance": glyph.advance,  # type: ignore[attr-defined]
        "size": glyph.size,  # type: ignore[attr-defined]
        "xOffset": glyph.x_offset,  # type: ignore[attr-defined]
        "penX": geometry.pen_x,
        "scale": geometry.scale,
        "baselinePx": scene.baseline_px,
        "bboxPx": list(geometry.bbox),
    }


def _tile_dependency(
    scene: Scene, spec_index: int, tx: int, ty: int, glyphs: list[dict]
) -> dict:
    return {
        "schema": SCHEMA_TILE_DEPS,
        "algorithm": ALGORITHM_VERSION,
        "canvas": {"width": scene.width, "height": scene.height},
        "tile": {
            "index": spec_index,
            "x": tx,
            "y": ty,
            "width": min(scene.tile_width, scene.width - tx * scene.tile_width),
            "height": min(scene.tile_height, scene.height - ty * scene.tile_height),
        },
        "supersampling": scene.supersampling,
        "flatten": {
            "tolerancePx": flatten_tolerance(scene),
            "maxDepth": MAX_SUBDIVISION_DEPTH,
        },
        "font": {
            "unitsPerEm": scene.units_per_em,
            "baselineOffset": scene.baseline_offset,
            "baselinePx": scene.baseline_px,
        },
        "layout": {"marginX": scene.margin_x, "spacing": scene.spacing},
        "glyphs": glyphs,
    }


def build_tile_specs(
    scene: Scene, geometries: list[GlyphGeometry] | None = None
) -> list[TileSpec]:
    if geometries is None:
        geometries = build_glyph_geometries(scene)
    glyphs_by_index = scene.glyphs
    by_tile: dict[int, list[GlyphGeometry]] = {
        index: [] for index in range(scene.tile_count)
    }
    for geometry in geometries:
        for tile_index in geometry.tile_indices:
            by_tile[tile_index].append(geometry)

    specs: list[TileSpec] = []
    for ty in range(scene.tile_rows):
        for tx in range(scene.tile_columns):
            index = ty * scene.tile_columns + tx
            tile_glyphs = sorted(by_tile[index], key=lambda item: item.glyph_index)
            glyph_deps = [
                _glyph_dependency(scene, glyphs_by_index[item.glyph_index], item)
                for item in tile_glyphs
            ]
            dependency = _tile_dependency(scene, index, tx, ty, glyph_deps)
            version = sha256_value(
                {"kind": "glyph-raster-tile-version/v1", "dependency": dependency}
            )
            specs.append(
                TileSpec(
                    index=index,
                    tx=tx,
                    ty=ty,
                    x0=tx * scene.tile_width,
                    y0=ty * scene.tile_height,
                    width=min(scene.tile_width, scene.width - tx * scene.tile_width),
                    height=min(
                        scene.tile_height, scene.height - ty * scene.tile_height
                    ),
                    dependency=dependency,
                    version=version,
                    glyph_ids=tuple(item.glyph_id for item in tile_glyphs),
                )
            )
    return specs


def _rasterize_glyph_tile(
    geometry: GlyphGeometry,
    spec: TileSpec,
    supersampling: int,
) -> np.ndarray:
    width = spec.width
    height = spec.height
    sub_width = width * supersampling
    counts = np.zeros((height, width), dtype=np.uint32)
    if geometry.edges.size == 0:
        return counts.astype(np.uint8)

    edges = geometry.edges
    x1 = edges[:, 0]
    y1 = edges[:, 1]
    x2 = edges[:, 2]
    y2 = edges[:, 3]
    directions = edges[:, 4]
    tile_x1 = spec.x0 + width
    tile_y1 = spec.y0 + height

    for sub_y in range(height * supersampling):
        y = spec.y0 + (sub_y + 0.5) / supersampling
        crossing_mask = ((y1 <= y) & (y2 > y)) | ((y2 <= y) & (y1 > y))
        if not np.any(crossing_mask):
            continue
        active_x1 = x1[crossing_mask]
        active_y1 = y1[crossing_mask]
        active_x2 = x2[crossing_mask]
        active_y2 = y2[crossing_mask]
        active_directions = directions[crossing_mask]
        crossings = active_x1 + (y - active_y1) * (
            (active_x2 - active_x1) / (active_y2 - active_y1)
        )
        order = np.argsort(crossings, kind="stable")
        sorted_x = crossings[order]
        sorted_directions = active_directions[order].astype(np.int32)
        winding = 0
        span_start = 0.0
        has_span = False
        diff = np.zeros(sub_width + 1, dtype=np.int16)
        for crossing_x, direction in zip(sorted_x, sorted_directions):
            before = winding
            winding += int(direction)
            if before == 0 and winding != 0:
                span_start = float(crossing_x)
                has_span = True
            elif before != 0 and winding == 0 and has_span:
                left = max(span_start, float(spec.x0))
                right = min(float(crossing_x), float(tile_x1))
                has_span = False
                if right <= left:
                    continue
                local_left = left - spec.x0
                local_right = right - spec.x0
                sub_start = math.ceil(local_left * supersampling - 0.5)
                sub_end = math.ceil(local_right * supersampling - 0.5)
                sub_start = max(0, min(sub_width, sub_start))
                sub_end = max(0, min(sub_width, sub_end))
                if sub_end > sub_start:
                    diff[sub_start] += 1
                    diff[sub_end] -= 1
        covered = np.cumsum(diff[:-1]) > 0
        if np.any(covered):
            row_counts = covered.reshape(width, supersampling).sum(axis=1)
            counts[sub_y // supersampling] += row_counts.astype(np.uint32)

    denominator = supersampling * supersampling
    return ((counts.astype(np.uint64) * 255 + denominator // 2) // denominator).astype(
        np.uint8
    )


def rasterize_tile(
    scene: Scene,
    spec: TileSpec,
    geometries: list[GlyphGeometry],
) -> np.ndarray:
    result = np.zeros((spec.height, spec.width), dtype=np.uint8)
    glyph_map = {geometry.glyph_id: geometry for geometry in geometries}
    for glyph_id in spec.glyph_ids:
        glyph_pixels = _rasterize_glyph_tile(
            glyph_map[glyph_id], spec, scene.supersampling
        )
        np.maximum(result, glyph_pixels, out=result)
    return result


def rasterize_scene_tiles(scene: Scene) -> tuple[list[TileSpec], list[np.ndarray]]:
    geometries = build_glyph_geometries(scene)
    specs = build_tile_specs(scene, geometries)
    return specs, [rasterize_tile(scene, spec, geometries) for spec in specs]


def assemble_tiles(scene: Scene, specs: list[TileSpec], tiles: list[np.ndarray]) -> np.ndarray:
    canvas = np.zeros((scene.height, scene.width), dtype=np.uint8)
    by_key = {spec.key: pixels for spec, pixels in zip(specs, tiles)}
    for spec in specs:
        pixels = by_key[spec.key]
        canvas[spec.y0 : spec.y0 + spec.height, spec.x0 : spec.x0 + spec.width] = pixels
    return canvas


def glyph_tile_counts(scene: Scene, specs: list[TileSpec]) -> dict[str, int]:
    result = {glyph.glyph_id: 0 for glyph in scene.glyphs}
    for spec in specs:
        for glyph_id in spec.glyph_ids:
            result[glyph_id] += 1
    return result
