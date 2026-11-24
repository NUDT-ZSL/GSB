"""High-level engine orchestration.

The engine ties together validation, deterministic rendering, the dual-budget
scheduler, tile persistence and edit-driven incremental re-rendering.  It is
the single place that understands exception injection, graceful versus hard
termination and the atomic publication of final images.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

from . import digest, edits as edit_mod, errors, scheduler, storage
from .model import Edit, TaskSpec, Terrain, TileSpec
from .render import render_tile, sample_buffer_bytes, tile_grid_extent
from .validate import _load_json


INTERRUPT_MARKER = "interrupted.json"


def load_injections(path: Optional[Path]) -> Dict[str, Any]:
    if path is None:
        return {}
    data = _load_json(path)
    return data if isinstance(data, dict) else {}


@dataclass
class TileOutcome:
    tile_id: str
    status: str  # rendered | reused | failed
    version: str
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    round_index: Optional[int] = None


class Engine:
    def __init__(self, task: TaskSpec, root: Path,
                 injections: Optional[Dict[str, Any]] = None,
                 stop_event=None) -> None:
        self.task = task
        self.root = Path(root)
        self.injections = injections or {}
        self.stop_event = stop_event
        self._digest_failure: Optional[errors.EngineError] = None
        if self.injections.get("tile_not_divisible"):
            raise errors.fail(
                errors.E_TILE_DIVISIBILITY,
                "injected tile divisibility failure",
                detail=self.injections.get("tile_not_divisible"))
        self.terrain_version = digest.digest_heights(task.terrain.heights)
        self.version = storage.parameters_digest(task, self.terrain_version)
        self.tile_by_id = {tile.tile_id: tile for tile in task.tiles}
        self.dependencies: Dict[str, Dict[str, int]] = {}
        for tile in task.tiles:
            min_i, min_j, max_i, max_j = tile_grid_extent(task, tile)
            self.dependencies[tile.tile_id] = {
                "min_i": min_i, "min_j": min_j,
                "max_i": max_i, "max_j": max_j}

    # ------------------------------------------------------------------
    # Injection helpers
    # ------------------------------------------------------------------
    def _injection_for(self, kind: str, tile_id: str) -> Optional[Dict[str, Any]]:
        for item in self.injections.get(kind, []):
            if isinstance(item, dict) and item.get("tile") == tile_id:
                return item
        return None

    def _check_height_mismatch(self, tile: TileSpec) -> None:
        spec = self._injection_for("height_count_mismatch", tile.tile_id)
        if spec is not None:
            declared = int(spec.get("declared",
                                    tile.width * tile.height + 1))
            raise errors.fail(
                errors.E_HEIGHT_COUNT,
                "injected height count mismatch for tile",
                tile=tile.tile_id, declared=declared,
                actual=tile.width * tile.height)

    # ------------------------------------------------------------------
    # Single tile pipeline
    # ------------------------------------------------------------------
    def render_one(self, tile: TileSpec) -> None:
        """Render and persist one tile, honouring cache and injections."""
        self._check_height_mismatch(tile)
        corrupt = self._injection_for("corrupt_digest", tile.tile_id)
        try:
            stored = storage.load_tile(
                self.task, self.root, tile, self.version)
        except errors.EngineError as exc:
            if exc.code != errors.E_DIGEST:
                raise
            stored = None
            self._digest_failure = exc
        if stored is not None:
            return

        gray, depth, sample_buffer = render_tile(self.task, tile)
        storage.commit_tile(
            self.task, self.root, tile, gray, depth,
            self.dependencies[tile.tile_id], self.version,
            corrupt_digest=corrupt is not None)
        # An intentionally corrupted tile fails its own post-write check.
        if corrupt is not None:
            raise errors.fail(
                errors.E_DIGEST,
                "injected corrupt tile digest",
                tile=tile.tile_id)

    def load_any_version(self, tile: TileSpec):
        """Load a committed tile regardless of version.

        A digest-valid tile from a previous terrain generation can be carried
        forward when an edit did not intersect its dependency range.  Damaged
        data still raises ``E_DIGEST`` and is never silently trusted.
        """
        state_dir = storage.state_path(self.task, self.root)
        manifest_path = state_dir / f"{tile.tile_id}.manifest.json"
        if not manifest_path.exists():
            return None
        import json
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        return storage.load_tile(
            self.task, self.root, tile, manifest["version"])

    def carry_forward(self, tile: TileSpec) -> bool:
        """Re-publish an unchanged tile under the current version id."""
        stored = self.load_any_version(tile)
        if stored is None:
            return False
        storage.commit_tile(
            self.task, self.root, tile, stored.gray, stored.depth,
            stored.dependency, self.version)
        return True

    def try_reuse(self, tile: TileSpec) -> Optional[storage.StoredTile]:
        """Return a valid cached tile or raise/record an E_DIGEST failure."""
        try:
            return storage.load_tile(
                self.task, self.root, tile, self.version)
        except errors.EngineError as exc:
            if exc.code != errors.E_DIGEST:
                raise
            self._digest_failure = exc
            return None

    # ------------------------------------------------------------------
    # Scheduled run over an explicit pending tile list
    # ------------------------------------------------------------------
    def run_tiles(self, pending: List[TileSpec],
                  clock_overrides: Optional[Dict[str, float]] = None
                  ) -> Dict[str, Any]:
        clock = scheduler.LogicalClock(clock_overrides)
        plans = scheduler.plan_rounds(self.task, pending, clock)
        outcomes: Dict[str, TileOutcome] = {}

        def worker(tile: TileSpec) -> None:
            self.render_one(tile)

        report = scheduler.execute_plans(
            self.task, plans, self.tile_by_id, worker, self.stop_event)
        for result in report.results:
            outcomes[result.tile_id] = TileOutcome(
                tile_id=result.tile_id,
                status="rendered" if result.ok else "failed",
                version=self.version,
                error_code=result.error_code,
                error_message=result.error_message,
                round_index=result.round_index)

        interrupted = self.stop_event is not None and self.stop_event.is_set()
        if interrupted:
            self._write_interrupt_marker(plans, outcomes)

        return {
            "version": self.version,
            "terrain_version": self.terrain_version,
            "rounds": [
                {
                    "round": plan.round_index,
                    "admitted": list(plan.admitted),
                    "deferred": list(plan.deferred),
                    "logical_elapsed_ms": plan.logical_elapsed_ms,
                    "peak_inflight_bytes": plan.peak_inflight_bytes,
                }
                for plan in report.rounds
            ],
            "outcomes": [self._outcome_dict(item)
                         for item in outcomes.values()],
            "interrupted": interrupted,
        }

    def _write_interrupt_marker(self, plans: List[scheduler.RoundPlan],
                                outcomes: Dict[str, TileOutcome]) -> None:
        state_dir = storage.state_path(self.task, self.root)
        state_dir.mkdir(parents=True, exist_ok=True)
        completed = sorted(
            tile_id for tile_id, outcome in outcomes.items()
            if outcome.status == "rendered")
        deferred: List[str] = []
        for plan in plans:
            deferred.extend(
                tile_id for tile_id in plan.deferred
                if tile_id not in completed)
        marker = {
            "exit_code": scheduler.GRACEFUL_EXIT_CODE,
            "reason": "graceful stop at tile boundary",
            "completed_tiles": completed,
            "deferred_tiles": sorted(set(deferred)),
            "version": self.version,
        }
        marker_path = state_dir / INTERRUPT_MARKER
        tmp = marker_path.with_name(marker_path.name + ".tmp")
        with tmp.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(marker, handle, sort_keys=True, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, marker_path)

    @staticmethod
    def _outcome_dict(outcome: TileOutcome) -> Dict[str, Any]:
        payload = {
            "tile": outcome.tile_id,
            "status": outcome.status,
            "version": outcome.version,
        }
        if outcome.error_code:
            payload["error_code"] = outcome.error_code
            payload["error_message"] = outcome.error_message
        if outcome.round_index is not None:
            payload["round"] = outcome.round_index
        return payload

    # ------------------------------------------------------------------
    # Full / preview / incremental entry points
    # ------------------------------------------------------------------
    def preview(self, tile_id: str) -> Dict[str, Any]:
        tile = self.tile_by_id.get(tile_id)
        if tile is None:
            raise errors.fail(errors.E_SCHEMA,
                              "unknown tile requested for preview",
                              tile=tile_id)
        cached = self.try_reuse(tile)
        if cached is not None:
            return {
                "tile": tile_id,
                "status": "reused",
                "version": self.version,
                "dependency": cached.dependency,
            }
        self.render_one(tile)
        return {"tile": tile_id, "status": "rendered",
                "version": self.version,
                "dependency": self.dependencies[tile_id]}

    def render_all(self,
                   clock_overrides: Optional[Dict[str, float]] = None
                   ) -> Dict[str, Any]:
        pending: List[TileSpec] = []
        reused: List[str] = []
        digest_failures: List[Dict[str, Any]] = []
        self._digest_failure: Optional[errors.EngineError] = None
        for tile in self.task.tiles:
            stored = self.try_reuse(tile)
            if stored is None:
                if self._digest_failure is not None:
                    digest_failures.append(
                        self._digest_failure.to_dict()["error"])
                    self._digest_failure = None
                pending.append(tile)
            else:
                reused.append(tile.tile_id)
        run = self.run_tiles(pending, clock_overrides)
        run["reused_tiles"] = sorted(reused)
        run["digest_failures"] = digest_failures
        run["dependencies"] = dict(sorted(self.dependencies.items()))
        run["memory_bytes_per_tile"] = {
            tile.tile_id: sample_buffer_bytes(tile, self.task.samples)
            for tile in self.task.tiles}
        return run

    def incremental(self, previous_terrain: Terrain,
                    edit_list: List[Edit],
                    clock_overrides: Optional[Dict[str, float]] = None
                    ) -> Dict[str, Any]:
        """Apply edits, compute the invalidation closure and re-render."""
        invalidation: List[Dict[str, Any]] = []
        for edit in edit_list:
            try:
                edit_mod.validate_edit_bounds(edit, previous_terrain)
            except errors.EngineError as exc:
                invalidation.append({
                    "edit_seq": edit.seq,
                    "edit_batch": edit.batch,
                    "error_code": exc.code,
                    "error_message": exc.message,
                    "details": exc.details,
                })
                continue
            edit_mod.apply_edit(previous_terrain, edit)
            invalidation.extend(
                edit_mod.invalidate_for_edit(
                    edit, self.dependencies))

        invalid_tiles = sorted({
            item["tile"] for item in invalidation if "tile" in item})
        # Refresh version identifiers after terrain mutation.
        self.terrain_version = digest.digest_heights(
            previous_terrain.heights)
        self.version = storage.parameters_digest(
            self.task, self.terrain_version)

        all_ids = {tile.tile_id for tile in self.task.tiles}
        carried: List[str] = []
        for tile_id in sorted(all_ids - set(invalid_tiles)):
            if self.carry_forward(self.tile_by_id[tile_id]):
                carried.append(tile_id)
        pending = [self.tile_by_id[tile_id] for tile_id in invalid_tiles]
        run = self.run_tiles(pending, clock_overrides)
        failed = {item["tile"] for item in run["outcomes"]
                  if item["status"] == "failed"}
        reused = sorted(set(carried) - failed)
        run["invalidation"] = invalidation
        run["rerendered_tiles"] = invalid_tiles
        run["reused_tiles"] = reused
        run["rerender_count"] = len(invalid_tiles)
        run["reuse_count"] = len(reused)
        run["dependencies"] = dict(sorted(self.dependencies.items()))
        return run

    # ------------------------------------------------------------------
    # Final composite publication
    # ------------------------------------------------------------------
    def composite(self) -> Dict[str, np.ndarray]:
        gray_full = np.zeros((self.task.height_px, self.task.width_px),
                             dtype=np.uint8)
        depth_full = np.full((self.task.height_px, self.task.width_px),
                             -1.0, dtype=np.float32)
        for tile in self.task.tiles:
            stored = storage.load_tile(
                self.task, self.root, tile, self.version)
            if stored is None:
                raise errors.fail(
                    errors.E_STATE,
                    "cannot composite: tile is not committed",
                    tile=tile.tile_id)
            gray_full[tile.y:tile.y + tile.height,
                      tile.x:tile.x + tile.width] = stored.gray
            depth_full[tile.y:tile.y + tile.height,
                       tile.x:tile.x + tile.width] = stored.depth
        return {"gray": gray_full, "depth": depth_full}

    def publish(self, image_path: Path, depth_path: Path) -> Dict[str, Any]:
        existing = storage.read_pgm_header(image_path)
        images = self.composite()
        storage.write_pgm(image_path, images["gray"])
        storage.write_depth(depth_path, images["depth"])
        return {
            "image": str(image_path),
            "depth": str(depth_path),
            "width": self.task.width_px,
            "height": self.task.height_px,
            "version": self.version,
            "previous_header": existing,
        }

