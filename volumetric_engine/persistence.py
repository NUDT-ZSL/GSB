"""Version-isolated block state and digest validation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from .errors import EngineError, error
from .model import Scene
from .renderer import PixelResult, TRACE_COLUMNS, TileGeometry, TileResult
from .util import atomic_write_json, read_json, sha256_json


STATE_FORMAT = "gsb-state-v1"
BLOCK_FORMAT = "gsb-block-v1"
INTERRUPT_FORMAT = "gsb-interrupt-v1"
COMPLETED_FORMAT = "gsb-completed-v1"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def pixel_to_dict(pixel: PixelResult) -> dict[str, Any]:
    return {
        "x": pixel.x,
        "y": pixel.y,
        "max_abs_difference": pixel.max_abs_difference,
        "max_fixed_rounding_difference": pixel.max_fixed_rounding_difference,
        "diagnostic_exceeded": pixel.diagnostic_exceeded,
        "fixed_verdict": pixel.fixed_verdict,
        "first_exceed": pixel.first_exceed,
        "early_cutoff": pixel.early_cutoff,
        "fixed_stopped_by_cutoff": pixel.fixed_stopped_by_cutoff,
        "float_stopped_by_cutoff": pixel.float_stopped_by_cutoff,
        "fixed_stop_step": pixel.fixed_stop_step,
        "float_stop_step": pixel.float_stop_step,
        "samples": pixel.samples,
        "steps": pixel.steps,
    }


def dict_to_pixel(
    raw: dict[str, Any],
    *,
    x: int,
    y: int,
    gray: int,
    reference_gray: int,
    depth: float,
) -> PixelResult:
    return PixelResult(
        x=x,
        y=y,
        gray=gray,
        reference_gray=reference_gray,
        depth=depth,
        max_abs_difference=float(raw["max_abs_difference"]),
        max_fixed_rounding_difference=float(raw["max_fixed_rounding_difference"]),
        diagnostic_exceeded=bool(raw["diagnostic_exceeded"]),
        fixed_verdict=str(raw["fixed_verdict"]),
        first_exceed=raw.get("first_exceed"),
        early_cutoff=bool(raw["early_cutoff"]),
        fixed_stopped_by_cutoff=bool(raw["fixed_stopped_by_cutoff"]),
        float_stopped_by_cutoff=bool(raw["float_stopped_by_cutoff"]),
        fixed_stop_step=raw.get("fixed_stop_step"),
        float_stop_step=raw.get("float_stop_step"),
        samples=int(raw["samples"]),
        steps=int(raw["steps"]),
    )


def _tile_stats(pixels: Sequence[PixelResult], trace_row_count: int) -> dict[str, Any]:
    return {
        "pixels": len(pixels),
        "samples": sum(pixel.samples for pixel in pixels),
        "steps": sum(pixel.steps for pixel in pixels),
        "trace_rows": trace_row_count,
        "fixed_verdict_pass_pixels": sum(1 for pixel in pixels if pixel.fixed_verdict == "pass"),
        "fixed_verdict_exceeded_pixels": sum(1 for pixel in pixels if pixel.fixed_verdict != "pass"),
        "diagnostic_tolerance_exceeded_pixels": sum(
            1 for pixel in pixels if pixel.diagnostic_exceeded
        ),
        "first_exceed_pixels": sum(1 for pixel in pixels if pixel.first_exceed is not None),
        "early_cutoff_pixels": sum(1 for pixel in pixels if pixel.early_cutoff),
        "fixed_cutoff_pixels": sum(1 for pixel in pixels if pixel.fixed_stopped_by_cutoff),
        "max_abs_transmittance_difference": max(
            (pixel.max_abs_difference for pixel in pixels), default=0.0
        ),
        "max_fixed_rounding_difference": max(
            (pixel.max_fixed_rounding_difference for pixel in pixels), default=0.0
        ),
    }


def tile_to_block(result: TileResult, version_id: str) -> dict[str, Any]:
    tiles = result.tile
    payload: dict[str, Any] = {
        "format": BLOCK_FORMAT,
        "version_id": version_id,
        "tile": {
            "index": tiles.index,
            "x0": tiles.x0,
            "y0": tiles.y0,
            "width": tiles.width,
            "height": tiles.height,
        },
        "pixel_count_declared": tiles.pixel_count,
        "color": result.gray,
        "reference_color": result.reference_gray,
        "depth": result.depth,
        "pixel_diagnostics": [pixel_to_dict(pixel) for pixel in result.pixels],
        "trace": {
            "columns": list(TRACE_COLUMNS),
            "rows": result.trace_rows,
        },
        "error_stats": _tile_stats(result.pixels, len(result.trace_rows)),
    }
    payload["digest"] = "sha256:" + sha256_json(payload)
    return payload


def _verify_digest(block: dict[str, Any]) -> tuple[bool, str]:
    digest = block.get("digest")
    if not isinstance(digest, str) or not digest.startswith("sha256:"):
        return False, "missing or malformed digest"
    body = dict(block)
    body.pop("digest", None)
    actual = "sha256:" + sha256_json(body)
    if actual != digest:
        return False, f"digest mismatch: declared {digest}, actual {actual}"
    return True, actual


def block_to_tile(block: dict[str, Any], expected_version: str) -> tuple[TileResult | None, str | None]:
    if not isinstance(block, dict):
        return None, "block root is not an object"
    if block.get("format") != BLOCK_FORMAT:
        return None, f"unsupported block format {block.get('format')!r}"
    if block.get("version_id") != expected_version:
        return None, f"version mismatch: {block.get('version_id')!r} != {expected_version!r}"
    valid, digest_message = _verify_digest(block)
    if not valid:
        return None, digest_message
    try:
        tile_raw = block["tile"]
        tile = TileGeometry(
            index=int(tile_raw["index"]),
            x0=int(tile_raw["x0"]),
            y0=int(tile_raw["y0"]),
            width=int(tile_raw["width"]),
            height=int(tile_raw["height"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        return None, f"invalid tile metadata: {exc}"
    declared = block.get("pixel_count_declared")
    if declared != tile.pixel_count:
        return None, f"declared pixel count {declared!r} does not match tile {tile.pixel_count}"
    color = block.get("color")
    reference_color = block.get("reference_color")
    depth = block.get("depth")
    diagnostics = block.get("pixel_diagnostics")
    if not isinstance(color, list) or len(color) != tile.height:
        return None, "color row count does not match tile height"
    if not isinstance(reference_color, list) or len(reference_color) != tile.height:
        return None, "reference_color row count does not match tile height"
    if not isinstance(depth, list) or len(depth) != tile.height:
        return None, "depth row count does not match tile height"
    if not isinstance(diagnostics, list) or len(diagnostics) != tile.pixel_count:
        return None, "pixel diagnostics count does not match declared pixel count"
    for row in color:
        if not isinstance(row, list) or len(row) != tile.width:
            return None, "color row width does not match tile width"
    for row in reference_color:
        if not isinstance(row, list) or len(row) != tile.width:
            return None, "reference_color row width does not match tile width"
    for row in depth:
        if not isinstance(row, list) or len(row) != tile.width:
            return None, "depth row width does not match tile width"
    trace = block.get("trace")
    if not isinstance(trace, dict) or trace.get("columns") != list(TRACE_COLUMNS):
        return None, "trace columns are missing or incompatible"
    rows = trace.get("rows")
    if not isinstance(rows, list) or any(not isinstance(row, list) or len(row) != len(TRACE_COLUMNS) for row in rows):
        return None, "trace rows are malformed"
    pixels: list[PixelResult] = []
    try:
        for local_y in range(tile.height):
            for local_x in range(tile.width):
                index = local_y * tile.width + local_x
                pixels.append(
                    dict_to_pixel(
                        diagnostics[index],
                        x=tile.x0 + local_x,
                        y=tile.y0 + local_y,
                        gray=int(color[local_y][local_x]),
                        reference_gray=int(reference_color[local_y][local_x]),
                        depth=float(depth[local_y][local_x]),
                    )
                )
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        return None, f"invalid pixel diagnostics: {exc}"
    return TileResult(tile=tile, pixels=pixels, trace_rows=rows), None


@dataclass
class StateIssue:
    code: str
    message: str
    tile_index: int | None = None
    path: str | None = None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.tile_index is not None:
            payload["tile_index"] = self.tile_index
        if self.path is not None:
            payload["path"] = self.path
        return payload


class StateStore:
    def __init__(self, scene: Scene, version_id: str) -> None:
        self.scene = scene
        self.version_id = version_id
        self.root = scene.render.state_dir
        self.meta_path = self.root / "meta.json"
        self.version_dir = self.root / "blocks" / version_id
        self.interrupted_path = self.root / "interrupted.json"
        self.completed_path = self.root / "completed.json"
        self.version_matched = False
        self.prepare_issues: list[StateIssue] = []

    def prepare(self) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            self.version_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise error("E_STATE", f"cannot create state directory: {exc}", str(self.root)) from exc
        previous_version = None
        if self.meta_path.exists():
            try:
                meta = read_json(self.meta_path, "E_STATE")
                if isinstance(meta, dict):
                    previous_version = meta.get("active_version")
                else:
                    self.prepare_issues.append(
                        StateIssue("E_STATE", "state meta root is not an object", path=str(self.meta_path))
                    )
            except EngineError as exc:
                self.prepare_issues.append(
                    StateIssue("E_STATE", exc.message, path=str(self.meta_path))
                )
        self.version_matched = previous_version == self.version_id
        if not self.version_matched:
            meta = {
                "format": STATE_FORMAT,
                "active_version": self.version_id,
                "updated_utc": _utc_now(),
                "previous_version": previous_version,
            }
            atomic_write_json(self.meta_path, meta)

    def block_path(self, tile_index: int) -> Path:
        return self.version_dir / f"block_{tile_index:05d}.json"

    def load_reusable(self, tiles: Sequence[TileGeometry]) -> tuple[dict[int, TileResult], list[StateIssue]]:
        reusable: dict[int, TileResult] = {}
        issues = list(self.prepare_issues)
        if not self.version_matched:
            other_versions = [
                candidate
                for candidate in self.root.glob("blocks/*")
                if candidate.is_dir()
                and candidate.name != self.version_id
                and any(candidate.iterdir())
            ]
            if other_versions:
                issues.append(
                    StateIssue(
                        "E_VERSION",
                        "active state version changed; all tiles will be rendered again",
                        path=str(self.root / "blocks"),
                    )
                )
            return reusable, issues
        for tile in tiles:
            path = self.block_path(tile.index)
            if not path.exists():
                issues.append(StateIssue("E_DIGEST", "completed block is missing", tile.index, str(path)))
                continue
            try:
                block = read_json(path, "E_DIGEST")
            except EngineError as exc:
                issues.append(StateIssue("E_DIGEST", exc.message, tile.index, str(path)))
                continue
            result, message = block_to_tile(block, self.version_id)
            if result is None:
                issues.append(StateIssue("E_DIGEST", message or "invalid block", tile.index, str(path)))
                continue
            if result.tile != tile:
                issues.append(
                    StateIssue(
                        "E_DIGEST",
                        "persisted tile geometry does not match current tile layout",
                        tile.index,
                        str(path),
                    )
                )
                continue
            reusable[tile.index] = result
        return reusable, issues

    def persist(self, result: TileResult) -> dict[str, Any]:
        path = self.block_path(result.tile.index)
        if path.exists():
            try:
                existing = read_json(path, "E_STATE")
                existing_version = existing.get("version_id") if isinstance(existing, dict) else None
            except EngineError:
                existing_version = None
            if existing_version is not None and existing_version != self.version_id:
                raise error(
                    "E_STATE",
                    "refusing to overwrite a block with a different version id",
                    str(path),
                    existing_version=existing_version,
                    new_version=self.version_id,
                )
        block = tile_to_block(result, self.version_id)
        atomic_write_json(path, block)
        return {
            "tile_index": result.tile.index,
            "path": str(path),
            "digest": block["digest"],
            "pixel_count": result.tile.pixel_count,
        }

    def validate_block(
        self, tile: TileGeometry
    ) -> tuple[TileResult | None, StateIssue | None, str | None]:
        path = self.block_path(tile.index)
        if not path.exists():
            return None, StateIssue("E_DIGEST", "completed block is missing", tile.index, str(path)), None
        try:
            block = read_json(path, "E_DIGEST")
        except EngineError as exc:
            return None, StateIssue("E_DIGEST", exc.message, tile.index, str(path)), None
        result, message = block_to_tile(block, self.version_id)
        if result is None:
            return None, StateIssue("E_DIGEST", message or "invalid block", tile.index, str(path)), None
        if result.tile != tile:
            return (
                None,
                StateIssue(
                    "E_DIGEST",
                    "persisted tile geometry does not match current tile layout",
                    tile.index,
                    str(path),
                ),
                None,
            )
        digest = block.get("digest")
        return result, None, str(digest) if digest is not None else None

    def mark_interrupted(self, token, completed_tiles: Sequence[int]) -> None:
        payload = {
            "format": INTERRUPT_FORMAT,
            "version_id": self.version_id,
            "signal": token.signal_name,
            "exit_code": token.exit_code,
            "completed_tiles": sorted(completed_tiles),
            "timestamp_utc": _utc_now(),
        }
        atomic_write_json(self.interrupted_path, payload)

    def mark_completed(self, block_digests: dict[int, str], scheduler_stats: dict[str, Any]) -> None:
        previous_scheduler: dict[str, Any] = {}
        if self.completed_path.exists():
            try:
                previous = read_json(self.completed_path, "E_STATE")
                if isinstance(previous, dict) and previous.get("version_id") == self.version_id:
                    candidate = previous.get("scheduler")
                    if isinstance(candidate, dict):
                        previous_scheduler = candidate
            except EngineError:
                previous_scheduler = {}
        merged_scheduler = {
            "peak_inflight_bytes": max(
                int(previous_scheduler.get("peak_inflight_bytes", 0)),
                int(scheduler_stats.get("peak_inflight_bytes", 0)),
            ),
            "submitted_tiles": int(previous_scheduler.get("submitted_tiles", 0))
            + int(scheduler_stats.get("submitted_tiles", 0)),
            "completed_tiles": int(previous_scheduler.get("completed_tiles", 0))
            + int(scheduler_stats.get("completed_tiles", 0)),
            "max_workers": max(
                int(previous_scheduler.get("max_workers", 0)),
                int(scheduler_stats.get("max_workers", 0)),
            ),
            "memory_limit_bytes": int(scheduler_stats.get("memory_limit_bytes", 0)),
        }
        payload = {
            "format": COMPLETED_FORMAT,
            "version_id": self.version_id,
            "tiles": len(block_digests),
            "block_digests": {str(index): digest for index, digest in sorted(block_digests.items())},
            "scheduler": merged_scheduler,
            "timestamp_utc": _utc_now(),
        }
        atomic_write_json(self.completed_path, payload)
        try:
            self.interrupted_path.unlink(missing_ok=True)
        except OSError as exc:
            raise error("E_STATE", f"cannot clear interrupt marker: {exc}", str(self.interrupted_path)) from exc

    def previous_scheduler_stats(self) -> dict[str, Any]:
        if not self.completed_path.exists():
            return {}
        try:
            previous = read_json(self.completed_path, "E_STATE")
        except EngineError:
            return {}
        if not isinstance(previous, dict) or previous.get("version_id") != self.version_id:
            return {}
        scheduler = previous.get("scheduler")
        return dict(scheduler) if isinstance(scheduler, dict) else {}

