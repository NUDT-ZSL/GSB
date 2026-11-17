from __future__ import annotations

import copy
import hashlib
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .errors import EngineError

SCHEMA_SCENE = "glyph-raster-scene/v1"
SCHEMA_OUTLINES = "glyph-outlines/v1"
ALGORITHM_VERSION = "adaptive-quadratic-scanline/v1"


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_value(value: Any) -> str:
    return sha256_bytes(canonical_json(value))


@dataclass(frozen=True)
class Segment:
    kind: str
    to: int
    control: int | None = None

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"type": self.kind, "to": self.to}
        if self.control is not None:
            result["control"] = self.control
        return result


@dataclass(frozen=True)
class Contour:
    start: int
    closed: bool
    segments: tuple[Segment, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "start": self.start,
            "closed": self.closed,
            "segments": [segment.as_dict() for segment in self.segments],
        }


@dataclass
class Glyph:
    glyph_id: str
    advance: float
    points: list[tuple[float, float]]
    contours: list[Contour]
    size: float
    x_offset: float

    def content_dict(self) -> dict[str, Any]:
        return {
            "id": self.glyph_id,
            "advance": self.advance,
            "points": [[x, y] for x, y in self.points],
            "contours": [contour.as_dict() for contour in self.contours],
            "size": self.size,
            "xOffset": self.x_offset,
        }


@dataclass(frozen=True)
class Edit:
    op: str
    glyph_id: str
    values: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        result = {"op": self.op, "glyph": self.glyph_id}
        result.update(self.values)
        return result


@dataclass
class Scene:
    scene_path: Path
    outline_path: Path
    workspace: Path
    units_per_em: float
    baseline_offset: float
    margin_x: float
    spacing: float
    width: int
    height: int
    supersampling: int
    tile_width: int
    tile_height: int
    concurrency: int
    memory_limit_bytes: int
    state_dir: Path
    output_path: Path
    report_path: Path
    glyphs: list[Glyph]
    edits: list[Edit] = field(default_factory=list)

    @property
    def baseline_px(self) -> float:
        return float(self.height) - self.baseline_offset

    @property
    def tile_columns(self) -> int:
        return (self.width + self.tile_width - 1) // self.tile_width

    @property
    def tile_rows(self) -> int:
        return (self.height + self.tile_height - 1) // self.tile_height

    @property
    def tile_count(self) -> int:
        return self.tile_columns * self.tile_rows

    @property
    def max_tile_sample_bytes(self) -> int:
        full = self.tile_width * self.tile_height * self.supersampling**2
        edge_w = min(self.tile_width, self.width)
        edge_h = min(self.tile_height, self.height)
        edge = edge_w * edge_h * self.supersampling**2
        return max(full, edge)

    def content_version(self) -> str:
        payload = {
            "schema": SCHEMA_SCENE,
            "algorithm": ALGORITHM_VERSION,
            "font": {
                "unitsPerEm": self.units_per_em,
                "baselineOffset": self.baseline_offset,
            },
            "layout": {"marginX": self.margin_x, "spacing": self.spacing},
            "resolution": {"width": self.width, "height": self.height},
            "supersampling": self.supersampling,
            "tile": {"width": self.tile_width, "height": self.tile_height},
            "glyphs": [glyph.content_dict() for glyph in self.glyphs],
        }
        return sha256_value(payload)

    def clone(self) -> "Scene":
        return copy.deepcopy(self)

    def with_edits_applied(self) -> "Scene":
        result = self.clone()
        if not result.edits:
            return result
        by_id = {glyph.glyph_id: glyph for glyph in result.glyphs}
        for edit in result.edits:
            glyph = by_id[edit.glyph_id]
            if edit.op == "set_point":
                point_index = int(edit.values["point"])
                glyph.points[point_index] = (
                    float(edit.values["x"]),
                    float(edit.values["y"]),
                )
            elif edit.op == "set_size":
                glyph.size = float(edit.values["size"])
            elif edit.op == "set_x_offset":
                glyph.x_offset = float(edit.values["xOffset"])
            else:  # Defensive: validated before this method is called.
                raise EngineError("E_EDIT", f"unsupported edit operation: {edit.op}")
        result.edits = []
        return result


def _reject_constant(token: str) -> None:
    raise ValueError(f"non-finite JSON constant is not allowed: {token}")


def load_json_file(path: Path, code: str = "E_JSON") -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise EngineError(code, f"cannot read JSON file: {path}", {"path": str(path)}) from exc
    try:
        return json.loads(text, parse_constant=_reject_constant)
    except (json.JSONDecodeError, ValueError) as exc:
        raise EngineError(code, f"invalid JSON in {path}: {exc}", {"path": str(path)}) from exc


def _require_object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise EngineError("E_SCHEMA", f"{name} must be an object")
    return value


def _require_list(value: Any, name: str, *, nonempty: bool = False) -> list[Any]:
    if not isinstance(value, list):
        raise EngineError("E_SCHEMA", f"{name} must be an array")
    if nonempty and not value:
        raise EngineError("E_VALIDATION", f"{name} must not be empty")
    return value


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EngineError("E_VALIDATION", f"{name} must be a finite number")
    try:
        result = float(value)
    except (OverflowError, ValueError) as exc:
        raise EngineError("E_VALIDATION", f"{name} must be a finite number") from exc
    if not math.isfinite(result):
        raise EngineError("E_VALIDATION", f"{name} must be a finite number")
    return result


def _positive_number(value: Any, name: str) -> float:
    result = _number(value, name)
    if result <= 0:
        raise EngineError("E_VALIDATION", f"{name} must be greater than zero")
    return result


def _nonnegative_number(value: Any, name: str) -> float:
    result = _number(value, name)
    if result < 0:
        raise EngineError("E_VALIDATION", f"{name} must be greater than or equal to zero")
    return result


def _integer(value: Any, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise EngineError("E_VALIDATION", f"{name} must be an integer")
    if value < minimum:
        raise EngineError("E_VALIDATION", f"{name} must be at least {minimum}")
    return value


def _path_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise EngineError("E_SCHEMA", f"{name} must be a non-empty path string")
    return value


def _workspace_path(workspace: Path, value: Any, name: str) -> Path:
    raw = _path_string(value, name)
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = workspace / candidate
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(workspace)
    except ValueError as exc:
        raise EngineError(
            "E_PATH",
            f"{name} must resolve inside the workspace",
            {"path": str(resolved), "workspace": str(workspace)},
        ) from exc
    return resolved


def _input_path(
    workspace: Path, scene_path: Path, value: Any, name: str
) -> Path:
    raw = _path_string(value, name)
    candidate = Path(raw)
    if candidate.is_absolute():
        return candidate.resolve(strict=False)
    scene_relative = (scene_path.parent / candidate).resolve(strict=False)
    workspace_relative = (workspace / candidate).resolve(strict=False)
    if scene_relative.exists():
        return scene_relative
    return workspace_relative


def _parse_point(value: Any, name: str) -> tuple[float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise EngineError("E_VALIDATION", f"{name} must be an [x, y] pair")
    return _number(value[0], f"{name}[0]"), _number(value[1], f"{name}[1]")


def _parse_glyph(raw: Any, index: int) -> Glyph:
    obj = _require_object(raw, f"glyphs[{index}]")
    glyph_id = obj.get("id")
    if not isinstance(glyph_id, str) or not glyph_id:
        raise EngineError("E_VALIDATION", f"glyphs[{index}].id must be a non-empty string")
    advance = _nonnegative_number(obj.get("advance"), f"glyph {glyph_id}.advance")
    size = _positive_number(obj.get("size"), f"glyph {glyph_id}.size")
    x_offset = _number(obj.get("xOffset", 0), f"glyph {glyph_id}.xOffset")
    raw_points = _require_list(obj.get("points"), f"glyph {glyph_id}.points", nonempty=True)
    points = [_parse_point(point, f"glyph {glyph_id}.points[{i}]") for i, point in enumerate(raw_points)]
    raw_contours = _require_list(
        obj.get("contours"), f"glyph {glyph_id}.contours", nonempty=True
    )
    contours: list[Contour] = []
    for contour_index, raw_contour in enumerate(raw_contours):
        contour_obj = _require_object(
            raw_contour, f"glyph {glyph_id}.contours[{contour_index}]"
        )
        start = _integer(
            contour_obj.get("start"),
            f"glyph {glyph_id}.contours[{contour_index}].start",
        )
        if start >= len(points):
            raise EngineError(
                "E_VALIDATION",
                f"glyph {glyph_id} contour {contour_index} start index is out of range",
            )
        if contour_obj.get("closed") is not True:
            raise EngineError(
                "E_CONTOUR",
                f"glyph {glyph_id} contour {contour_index} must be closed",
            )
        raw_segments = _require_list(
            contour_obj.get("segments"),
            f"glyph {glyph_id}.contours[{contour_index}].segments",
            nonempty=True,
        )
        last = start
        segments: list[Segment] = []
        for segment_index, raw_segment in enumerate(raw_segments):
            segment_obj = _require_object(
                raw_segment,
                f"glyph {glyph_id} contour {contour_index} segment {segment_index}",
            )
            kind = segment_obj.get("type")
            if kind not in ("line", "quadratic"):
                raise EngineError(
                    "E_VALIDATION",
                    f"glyph {glyph_id} contour {contour_index} segment {segment_index} "
                    "has unsupported type",
                )
            to_index = _integer(
                segment_obj.get("to"),
                f"glyph {glyph_id} contour {contour_index} segment {segment_index}.to",
            )
            if to_index >= len(points):
                raise EngineError(
                    "E_VALIDATION",
                    f"glyph {glyph_id} contour {contour_index} segment {segment_index} "
                    "endpoint index is out of range",
                )
            control_index: int | None = None
            if kind == "quadratic":
                control_index = _integer(
                    segment_obj.get("control"),
                    f"glyph {glyph_id} contour {contour_index} "
                    f"segment {segment_index}.control",
                )
                if control_index >= len(points):
                    raise EngineError(
                        "E_VALIDATION",
                        f"glyph {glyph_id} contour {contour_index} segment "
                        f"{segment_index} control index is out of range",
                    )
            segments.append(Segment(kind, to_index, control_index))
            last = to_index
        if last != start:
            raise EngineError(
                "E_CONTOUR",
                f"glyph {glyph_id} contour {contour_index} does not return to its start point",
            )
        contours.append(Contour(start, True, tuple(segments)))

    if not any(contour.closed for contour in contours):
        raise EngineError("E_CONTOUR", f"glyph {glyph_id} has no closed contour")
    return Glyph(glyph_id, advance, points, contours, size, x_offset)


def _parse_edits(raw_edits: Any, glyphs: Iterable[Glyph]) -> list[Edit]:
    if raw_edits is None:
        return []
    raw_list = _require_list(raw_edits, "edits")
    glyph_map = {glyph.glyph_id: glyph for glyph in glyphs}
    edits: list[Edit] = []
    for index, raw_edit in enumerate(raw_list):
        try:
            obj = _require_object(raw_edit, f"edits[{index}]")
            op = obj.get("op")
            if op not in ("set_point", "set_size", "set_x_offset"):
                raise EngineError("E_EDIT", f"edits[{index}] has unsupported op: {op!r}")
            glyph_id = obj.get("glyph")
            if not isinstance(glyph_id, str) or glyph_id not in glyph_map:
                raise EngineError(
                    "E_EDIT",
                    f"edits[{index}] targets a glyph that does not exist",
                    {"glyph": glyph_id},
                )
            glyph = glyph_map[glyph_id]
            if op == "set_point":
                point_index = _integer(obj.get("point"), f"edits[{index}].point")
                if point_index >= len(glyph.points):
                    raise EngineError(
                        "E_EDIT",
                        f"edits[{index}] point index is out of range",
                        {"glyph": glyph_id, "point": point_index},
                    )
                x = _number(obj.get("x"), f"edits[{index}].x")
                y = _number(obj.get("y"), f"edits[{index}].y")
                edits.append(Edit(op, glyph_id, {"point": point_index, "x": x, "y": y}))
            elif op == "set_size":
                size = _positive_number(obj.get("size"), f"edits[{index}].size")
                edits.append(Edit(op, glyph_id, {"size": size}))
            else:
                x_offset = _number(obj.get("xOffset"), f"edits[{index}].xOffset")
                edits.append(Edit(op, glyph_id, {"xOffset": x_offset}))
        except EngineError as exc:
            if exc.code == "E_EDIT":
                raise
            raise EngineError(
                "E_EDIT",
                f"edits[{index}] is invalid: {exc.message}",
                {"editIndex": index, **exc.details},
            ) from exc
    return edits


def _load_outline_glyphs(path: Path) -> list[Glyph]:
    root_raw = load_json_file(path)
    root = _require_object(root_raw, "outline root")
    schema = root.get("schema", SCHEMA_OUTLINES)
    if schema != SCHEMA_OUTLINES:
        raise EngineError("E_SCHEMA", f"unsupported outline schema: {schema!r}")
    raw_glyphs = _require_list(root.get("glyphs"), "glyphs", nonempty=True)
    glyphs = [_parse_glyph(raw, index) for index, raw in enumerate(raw_glyphs)]
    ids = [glyph.glyph_id for glyph in glyphs]
    if len(ids) != len(set(ids)):
        raise EngineError("E_VALIDATION", "glyph ids must be unique")
    return glyphs


def _parse_dimension_pair(value: Any, name: str) -> tuple[int, int]:
    if isinstance(value, list):
        if len(value) != 2:
            raise EngineError("E_SCHEMA", f"{name} must contain exactly two dimensions")
        return _integer(value[0], f"{name}[0]", minimum=1), _integer(
            value[1], f"{name}[1]", minimum=1
        )
    obj = _require_object(value, name)
    return _integer(obj.get("width"), f"{name}.width", minimum=1), _integer(
        obj.get("height"), f"{name}.height", minimum=1
    )


@dataclass
class SceneBundle:
    base: Scene
    target: Scene
    apply_edits: bool

    @property
    def scene(self) -> Scene:
        return self.target


def load_scene_bundle(
    scene_path: str | Path,
    *,
    workspace: str | Path = ".",
    apply_edits: bool = False,
) -> SceneBundle:
    workspace_path = Path(workspace).resolve(strict=False)
    raw_scene_path = Path(scene_path)
    if not raw_scene_path.is_absolute():
        raw_scene_path = workspace_path / raw_scene_path
    scene_path_resolved = raw_scene_path.resolve(strict=False)

    root_raw = load_json_file(scene_path_resolved)
    root = _require_object(root_raw, "scene root")
    schema = root.get("schema", SCHEMA_SCENE)
    if schema != SCHEMA_SCENE:
        raise EngineError("E_SCHEMA", f"unsupported scene schema: {schema!r}")

    outline_path = _input_path(
        workspace_path, scene_path_resolved, root.get("outlineFile"), "outlineFile"
    )
    glyphs = _load_outline_glyphs(outline_path)

    font = _require_object(root.get("font"), "font")
    units_per_em = _positive_number(font.get("unitsPerEm"), "font.unitsPerEm")
    baseline_offset = _number(font.get("baselineOffset"), "font.baselineOffset")

    layout = _require_object(root.get("layout", {}), "layout")
    margin_x = _number(layout.get("marginX", 0), "layout.marginX")
    spacing = _nonnegative_number(layout.get("spacing", 0), "layout.spacing")

    width, height = _parse_dimension_pair(root.get("resolution"), "resolution")
    supersampling = _integer(root.get("supersampling"), "supersampling", minimum=1)
    raw_tile = root.get("tile", root.get("tileSize"))
    tile_width, tile_height = _parse_dimension_pair(raw_tile, "tile")

    concurrency = _integer(root.get("concurrency"), "concurrency", minimum=1)
    memory_limit_bytes = _integer(
        root.get("memoryLimitBytes"), "memoryLimitBytes", minimum=1
    )
    state_dir = _workspace_path(workspace_path, root.get("stateDir"), "stateDir")
    output_path = _workspace_path(workspace_path, root.get("output"), "output")
    report_path = _workspace_path(workspace_path, root.get("report"), "report")
    if output_path == report_path:
        raise EngineError("E_PATH", "output and report paths must differ")
    if output_path == state_dir or report_path == state_dir:
        raise EngineError("E_PATH", "output/report paths must differ from stateDir")

    edits = _parse_edits(root.get("edits", []), glyphs)

    base = Scene(
        scene_path=scene_path_resolved,
        outline_path=outline_path,
        workspace=workspace_path,
        units_per_em=units_per_em,
        baseline_offset=baseline_offset,
        margin_x=margin_x,
        spacing=spacing,
        width=width,
        height=height,
        supersampling=supersampling,
        tile_width=tile_width,
        tile_height=tile_height,
        concurrency=concurrency,
        memory_limit_bytes=memory_limit_bytes,
        state_dir=state_dir,
        output_path=output_path,
        report_path=report_path,
        glyphs=glyphs,
        edits=edits,
    )
    target = base.with_edits_applied() if apply_edits else base.clone()
    target.edits = []
    if base.max_tile_sample_bytes > memory_limit_bytes:
        raise EngineError(
            "E_MEMORY",
            "memoryLimitBytes is smaller than one full tile sampling buffer",
            {
                "memoryLimitBytes": memory_limit_bytes,
                "requiredBytes": base.max_tile_sample_bytes,
            },
        )
    return SceneBundle(base=base, target=target, apply_edits=apply_edits)
