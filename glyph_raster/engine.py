from __future__ import annotations

import concurrent.futures
import copy
import json
import signal
import threading
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any, Callable

import numpy as np

from .errors import EngineError
from .geometry import (
    GlyphGeometry,
    TileSpec,
    assemble_tiles,
    build_glyph_geometries,
    build_tile_specs,
    glyph_tile_counts,
    rasterize_scene_tiles,
    rasterize_tile,
)
from .model import SceneBundle, canonical_json, sha256_bytes
from .state import (
    SCHEMA_REPORT,
    StateLock,
    TileRecord,
    TileStore,
    commit_output_pair,
    encode_pgm,
    read_pgm,
    recover_output_transaction,
)

REASON_PRIORITY = {
    "geometry_bbox": 0,
    "size": 1,
    "x_offset": 2,
    "layout_shift": 3,
    "supersampling_change": 4,
    "resolution_change": 5,
    "scene_version_mismatch": 6,
    "digest": 7,
    "missing": 8,
    "dependency_changed": 9,
}


def _preferred_reason(current: str | None, candidate: str) -> str:
    if current is None:
        return candidate
    return (
        candidate
        if REASON_PRIORITY.get(candidate, 99) < REASON_PRIORITY.get(current, 99)
        else current
    )


@dataclass
class _RenderQueueResult:
    rendered: set[str]
    archived: int
    stopped: bool
    error: BaseException | None


class RasterEngine:
    def __init__(self, bundle: SceneBundle, *, on_tile: Callable[[TileSpec], None] | None = None):
        self.bundle = bundle
        self.base = bundle.base
        self.scene = bundle.target
        self.on_tile = on_tile
        self.stop_event = threading.Event()
        self.signal_number: int | None = None
        self._abort_internal = threading.Event()

    def request_stop(self, signum: int = signal.SIGINT) -> None:
        if not self.stop_event.is_set():
            self.signal_number = signum
            self.stop_event.set()

    @staticmethod
    def _signal_name(signum: int) -> str:
        try:
            return signal.Signals(signum).name
        except ValueError:
            return f"signal-{signum}"

    def _interrupted_result(self, completed: set[str], pending: set[str]) -> dict[str, Any]:
        signum = self.signal_number or signal.SIGINT
        exit_code = 128 + int(signum)
        store = TileStore(self.scene.state_dir)
        store.write_interrupted(
            {
                "status": "interrupted",
                "signal": int(signum),
                "signalName": self._signal_name(signum),
                "exitCode": exit_code,
                "contentVersion": self.scene.content_version(),
                "completedTiles": sorted(completed),
                "pendingTiles": sorted(pending),
                "reportWritten": False,
            }
        )
        return {
            "status": "interrupted",
            "code": "E_INTERRUPTED",
            "exitCode": exit_code,
            "signal": int(signum),
            "signalName": self._signal_name(signum),
            "contentVersion": self.scene.content_version(),
            "completedTiles": len(completed),
            "pendingTiles": len(pending),
            "stateDir": str(self.scene.state_dir),
            "reportWritten": False,
            "outputWritten": False,
        }

    @staticmethod
    def _validate_record_for_spec(record: TileRecord, spec: TileSpec) -> None:
        if record.version != spec.version:
            raise EngineError(
                "E_DIGEST",
                "tile version does not match the target dependency version",
                {"tile": spec.key},
            )
        if record.dependency != spec.dependency:
            raise EngineError(
                "E_DIGEST",
                "tile dependency record does not match the target dependency",
                {"tile": spec.key},
            )
        if record.width != spec.width or record.height != spec.height:
            raise EngineError(
                "E_DIGEST",
                "tile dimensions do not match the target tile",
                {"tile": spec.key},
            )
        if record.pixel_count != spec.width * spec.height:
            raise EngineError(
                "E_DIGEST",
                "tile declared pixel count is inconsistent",
                {"tile": spec.key},
            )

    def _old_tile_glyph_map(self, store: TileStore, specs: list[TileSpec]) -> dict[str, set[str]]:
        result: dict[str, set[str]] = {}
        for spec in specs:
            payload = store.read_tile_metadata(spec.key)
            glyphs: set[str] = set()
            if payload is not None:
                dependency = payload.get("dependency")
                if isinstance(dependency, dict):
                    raw_glyphs = dependency.get("glyphs")
                    if isinstance(raw_glyphs, list):
                        for raw_glyph in raw_glyphs:
                            if isinstance(raw_glyph, dict) and isinstance(
                                raw_glyph.get("id"), str
                            ):
                                glyphs.add(raw_glyph["id"])
            result[spec.key] = glyphs
        return result

    def _edit_reason_map(
        self,
        specs: list[TileSpec],
        geometries: list[GlyphGeometry],
        old_tile_glyphs: dict[str, set[str]],
    ) -> dict[str, str]:
        base_geometries = build_glyph_geometries(self.base)
        base_by_id = {geometry.glyph_id: geometry for geometry in base_geometries}
        glyph_reasons: dict[str, str] = {}
        edited_glyphs: set[str] = set()
        for edit in self.base.edits:
            reason = {
                "set_point": "geometry_bbox",
                "set_size": "size",
                "set_x_offset": "x_offset",
            }[edit.op]
            glyph_reasons[edit.glyph_id] = _preferred_reason(
                glyph_reasons.get(edit.glyph_id), reason
            )
            edited_glyphs.add(edit.glyph_id)
        for geometry in geometries:
            old = base_by_id.get(geometry.glyph_id)
            if (
                old is not None
                and old.pen_x != geometry.pen_x
                and geometry.glyph_id not in edited_glyphs
            ):
                glyph_reasons[geometry.glyph_id] = _preferred_reason(
                    glyph_reasons.get(geometry.glyph_id), "layout_shift"
                )

        target_tiles: dict[str, set[str]] = {}
        for spec in specs:
            for glyph_id in spec.glyph_ids:
                target_tiles.setdefault(glyph_id, set()).add(spec.key)
        reasons: dict[str, str] = {}
        for glyph_id, reason in glyph_reasons.items():
            keys = set(target_tiles.get(glyph_id, set()))
            for key, old_glyphs in old_tile_glyphs.items():
                if glyph_id in old_glyphs:
                    keys.add(key)
            for key in keys:
                reasons[key] = _preferred_reason(reasons.get(key), reason)
        return reasons

    def _inspect_initial_state(
        self,
        store: TileStore,
        specs: list[TileSpec],
        manifest: dict | None,
        manifest_error: str | None,
        marker_exists: bool,
    ) -> tuple[
        dict[str, np.ndarray],
        list[TileSpec],
        dict[str, str],
        list[dict],
        bool,
        str,
    ]:
        target_version = self.scene.content_version()
        base_version = self.base.content_version()
        if manifest is not None and manifest.get("contentVersion") == target_version:
            mode = "reuse"
        elif (
            self.bundle.apply_edits
            and manifest is not None
            and manifest.get("contentVersion") == base_version
        ):
            mode = "incremental"
        elif manifest is None:
            mode = "recover"
        else:
            mode = "redo"

        completed_expected = mode == "reuse" and not marker_exists
        old_tile_glyphs = self._old_tile_glyph_map(store, specs)
        geometries = build_glyph_geometries(self.scene)
        reason_hints = (
            self._edit_reason_map(specs, geometries, old_tile_glyphs)
            if mode == "incremental"
            else {}
        )

        reused_pixels: dict[str, np.ndarray] = {}
        render_specs: list[TileSpec] = []
        reasons: dict[str, str] = {}
        digest_issues: list[dict] = []
        for spec in specs:
            path = store.tile_path(spec.key)
            if not path.exists():
                reason = reason_hints.get(spec.key, "missing")
                reasons[spec.key] = reason
                render_specs.append(spec)
                if completed_expected:
                    digest_issues.append(
                        {"tile": spec.key, "reason": "missing", "message": "tile file is missing"}
                    )
                continue
            if mode == "redo":
                reasons[spec.key] = "scene_version_mismatch"
                render_specs.append(spec)
                continue
            try:
                record = store.read_tile(spec.key)
            except EngineError as exc:
                if exc.code != "E_DIGEST":
                    raise
                digest_issues.append(
                    {"tile": spec.key, "reason": "digest", "message": exc.message}
                )
                reasons[spec.key] = "digest"
                render_specs.append(spec)
                continue
            try:
                self._validate_record_for_spec(record, spec)
            except EngineError as exc:
                if exc.code != "E_DIGEST":
                    raise
                if mode == "reuse" and completed_expected:
                    digest_issues.append(
                        {"tile": spec.key, "reason": "digest", "message": exc.message}
                    )
                    reason = "digest"
                else:
                    reason = reason_hints.get(spec.key, "dependency_changed")
                reasons[spec.key] = reason
                render_specs.append(spec)
                continue
            reused_pixels[spec.key] = record.pixels

        if manifest_error:
            digest_issues.append(
                {
                    "tile": "*",
                    "reason": "manifest",
                    "message": f"manifest was ignored: {manifest_error}",
                }
            )
        return reused_pixels, render_specs, reasons, digest_issues, completed_expected, mode

    def _run_render_queue(
        self,
        store: TileStore,
        specs: list[TileSpec],
        geometries: list[GlyphGeometry],
        initial_reasons: dict[str, str],
    ) -> _RenderQueueResult:
        rendered: set[str] = set()
        archived = 0
        if not specs:
            return _RenderQueueResult(rendered, archived, False, None)

        workers = min(self.scene.concurrency, len(specs))
        executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="glyph-tile"
        )
        active: dict[concurrent.futures.Future, TileSpec] = {}
        active_bytes = 0
        errors: list[tuple[int, BaseException]] = []
        position = 0
        stopped = False

        def sampling_bytes(spec: TileSpec) -> int:
            return spec.width * spec.height * self.scene.supersampling**2

        try:
            while position < len(specs) or active:
                while (
                    position < len(specs)
                    and len(active) < workers
                    and not self.stop_event.is_set()
                    and not self._abort_internal.is_set()
                ):
                    spec = specs[position]
                    required = sampling_bytes(spec)
                    if active_bytes + required > self.scene.memory_limit_bytes:
                        break
                    future = executor.submit(rasterize_tile, self.scene, spec, geometries)
                    active[future] = spec
                    active_bytes += required
                    position += 1

                if errors:
                    break
                if not active:
                    if position < len(specs):
                        spec = specs[position]
                        if sampling_bytes(spec) > self.scene.memory_limit_bytes:
                            errors.append(
                                (
                                    spec.index,
                                    EngineError(
                                        "E_MEMORY",
                                        "a tile sampling buffer exceeds the memory limit",
                                        {
                                            "tile": spec.key,
                                            "requiredBytes": sampling_bytes(spec),
                                            "memoryLimitBytes": self.scene.memory_limit_bytes,
                                        },
                                    ),
                                )
                            )
                    break

                done, _ = concurrent.futures.wait(
                    active,
                    return_when=concurrent.futures.FIRST_COMPLETED,
                )
                for future in sorted(
                    done, key=lambda item: active[item].index
                ):
                    spec = active.pop(future)
                    active_bytes -= sampling_bytes(spec)
                    try:
                        pixels = future.result()
                    except BaseException as exc:
                        errors.append((spec.index, exc))
                        self._abort_internal.set()
                        continue
                    try:
                        if store.persist_tile(
                            spec, pixels, self.scene.content_version()
                        ):
                            archived += 1
                    except BaseException as exc:
                        errors.append((spec.index, exc))
                        self._abort_internal.set()
                        continue
                    rendered.add(spec.key)
                    initial_reasons.setdefault(spec.key, "dependency_changed")
                    if self.on_tile is not None:
                        self.on_tile(spec)

                if self.stop_event.is_set():
                    stopped = True
                    break
                if self._abort_internal.is_set():
                    break
        finally:
            executor.shutdown(wait=True, cancel_futures=True)

        first_error: BaseException | None = None
        if errors:
            first_error = min(errors, key=lambda item: item[0])[1]
        return _RenderQueueResult(rendered, archived, stopped, first_error)

    def _final_digest_repair(
        self,
        store: TileStore,
        specs: list[TileSpec],
        geometries: list[GlyphGeometry],
        tile_pixels: dict[str, np.ndarray],
        rendered: set[str],
        reasons: dict[str, str],
        archived: int,
        digest_issues: list[dict],
    ) -> tuple[int, set[str]]:
        repair_specs: list[TileSpec] = []
        for spec in specs:
            try:
                record = store.read_tile(spec.key)
                self._validate_record_for_spec(record, spec)
            except EngineError as exc:
                if exc.code != "E_DIGEST":
                    raise
                digest_issues.append(
                    {"tile": spec.key, "reason": "digest", "message": exc.message}
                )
                reasons[spec.key] = "digest"
                tile_pixels.pop(spec.key, None)
                repair_specs.append(spec)
            else:
                tile_pixels[spec.key] = record.pixels

        if not repair_specs:
            return archived, rendered
        if self.stop_event.is_set():
            for spec in repair_specs:
                tile_pixels.pop(spec.key, None)
            return archived, rendered
        result = self._run_render_queue(store, repair_specs, geometries, reasons)
        rendered.update(result.rendered)
        archived += result.archived
        if result.error is not None:
            if isinstance(result.error, EngineError):
                raise result.error
            raise EngineError(
                "E_RENDER", f"tile repair failed: {result.error}", {"tile": repair_specs[0].key}
            )
        if result.stopped:
            for key in result.rendered:
                try:
                    tile_pixels[key] = store.read_tile(key).pixels
                except EngineError:
                    pass
            return archived, rendered
        for spec in repair_specs:
            try:
                record = store.read_tile(spec.key)
                self._validate_record_for_spec(record, spec)
            except EngineError as exc:
                raise EngineError(
                    "E_DIGEST",
                    f"tile remained invalid after repair: {spec.key}",
                    {"tile": spec.key, "error": exc.message},
                ) from exc
            tile_pixels[spec.key] = record.pixels
        return archived, rendered

    def _make_report(
        self,
        specs: list[TileSpec],
        rendered: set[str],
        reasons: dict[str, str],
        archived: int,
        digest_issues: list[dict],
        bitmap_replaced: bool,
        bitmap_sha: str,
        elapsed_ms: int,
    ) -> dict[str, Any]:
        tile_counts = glyph_tile_counts(self.scene, specs)
        per_glyph: dict[str, dict[str, Any]] = {
            glyph.glyph_id: {
                "tileCount": tile_counts.get(glyph.glyph_id, 0),
                "reusedTiles": 0,
                "invalidatedTiles": 0,
                "invalidReasons": {},
            }
            for glyph in self.scene.glyphs
        }
        for spec in specs:
            invalid = spec.key in rendered
            for glyph_id in spec.glyph_ids:
                entry = per_glyph[glyph_id]
                if invalid:
                    entry["invalidatedTiles"] += 1
                    reason = reasons.get(spec.key, "dependency_changed")
                    entry["invalidReasons"][reason] = (
                        entry["invalidReasons"].get(reason, 0) + 1
                    )
                else:
                    entry["reusedTiles"] += 1
        invalid_reasons = Counter(
            reasons.get(key, "dependency_changed") for key in rendered
        )
        code = "E_DIGEST" if digest_issues else "OK"
        return {
            "schema": SCHEMA_REPORT,
            "status": "recovered" if digest_issues else "ok",
            "code": code,
            "exitCode": 5 if digest_issues else 0,
            "contentVersion": self.scene.content_version(),
            "width": self.scene.width,
            "height": self.scene.height,
            "supersampling": self.scene.supersampling,
            "tileSize": {
                "width": self.scene.tile_width,
                "height": self.scene.tile_height,
            },
            "tiles": {
                "total": len(specs),
                "reused": len(specs) - len(rendered),
                "rendered": len(rendered),
                "invalidated": len(rendered),
                "archived": archived,
            },
            "invalidReasons": dict(sorted(invalid_reasons.items())),
            "perGlyph": per_glyph,
            "digestIssues": copy.deepcopy(digest_issues),
            "bitmap": {
                "path": str(self.scene.output_path),
                "replaced": bitmap_replaced,
                "sha256": bitmap_sha,
                "contentVersion": self.scene.content_version(),
            },
            "stateDir": str(self.scene.state_dir),
            "elapsedMs": elapsed_ms,
        }

    def render(self) -> dict[str, Any]:
        started = time.monotonic()
        target_version = self.scene.content_version()
        store = TileStore(self.scene.state_dir)
        with StateLock(self.scene.state_dir):
            store.ensure()
            store.clean_stale_temps()
            recovered_transaction = recover_output_transaction(self.scene.state_dir)
            manifest, manifest_error = store.load_manifest()
            marker_exists = store.interrupted_exists()
            specs = build_tile_specs(self.scene)
            geometries = build_glyph_geometries(self.scene)
            (
                tile_pixels,
                render_specs,
                reasons,
                digest_issues,
                _completed_expected,
                mode,
            ) = self._inspect_initial_state(
                store, specs, manifest, manifest_error, marker_exists
            )

            queue_result = self._run_render_queue(
                store, render_specs, geometries, reasons
            )
            rendered = set(queue_result.rendered)
            archived = queue_result.archived
            for spec in render_specs:
                if spec.key in rendered:
                    try:
                        tile_pixels[spec.key] = store.read_tile(spec.key).pixels
                    except EngineError:
                        pass
            pending = {
                spec.key for spec in render_specs if spec.key not in rendered
            }
            if queue_result.error is not None:
                if isinstance(queue_result.error, EngineError):
                    raise queue_result.error
                raise EngineError(
                    "E_RENDER",
                    f"tile rendering failed: {queue_result.error}",
                    {"completedTiles": sorted(rendered)},
                )
            if (queue_result.stopped or self.stop_event.is_set()) and pending:
                return self._interrupted_result(set(tile_pixels), pending)

            archived, rendered = self._final_digest_repair(
                store,
                specs,
                geometries,
                tile_pixels,
                rendered,
                reasons,
                archived,
                digest_issues,
            )
            if self.stop_event.is_set():
                pending = {spec.key for spec in specs if spec.key not in tile_pixels}
                if pending:
                    return self._interrupted_result(set(tile_pixels), pending)

            missing_pixels = [spec.key for spec in specs if spec.key not in tile_pixels]
            if missing_pixels:
                raise EngineError(
                    "E_STATE",
                    "tile assembly is missing completed pixels",
                    {"tiles": missing_pixels[:20]},
                )
            ordered_tiles = [tile_pixels[spec.key] for spec in specs]
            canvas = assemble_tiles(self.scene, specs, ordered_tiles)
            bitmap_sha = sha256_bytes(canvas.tobytes(order="C"))

            bitmap_replaced = False
            existing: Any = None
            try:
                existing = read_pgm(self.scene.output_path, load_pixels=True)
            except EngineError:
                existing = None
            if (
                existing is None
                or existing.info.width != self.scene.width
                or existing.info.height != self.scene.height
                or existing.info.content_version != target_version
            ):
                bitmap_replaced = True
            bitmap_data = (
                encode_pgm(canvas, target_version) if bitmap_replaced else None
            )

            elapsed_ms = int((time.monotonic() - started) * 1000)
            report = self._make_report(
                specs,
                rendered,
                reasons,
                archived,
                digest_issues,
                bitmap_replaced,
                bitmap_sha,
                elapsed_ms,
            )
            report["recoveredTransaction"] = recovered_transaction
            report["mode"] = mode
            report_data = canonical_json(report) + b"\n"
            commit_output_pair(
                self.scene.state_dir,
                self.scene.output_path,
                bitmap_data,
                self.scene.report_path,
                report_data,
            )
            store.write_manifest(target_version, self.scene, specs)
            store.clear_interrupted()

        result = dict(report)
        result["reportPath"] = str(self.scene.report_path)
        return result

    def verify(self, *, full_check: bool = False) -> dict[str, Any]:
        store = TileStore(self.scene.state_dir)
        issues: list[dict] = []
        if (self.scene.state_dir / "output-transaction.json").exists():
            raise EngineError(
                "E_STATE", "an unfinished output transaction requires a render recovery"
            )
        specs = build_tile_specs(self.scene)
        tile_pixels: dict[str, np.ndarray] = {}
        for spec in specs:
            try:
                record = store.read_tile(spec.key)
                self._validate_record_for_spec(record, spec)
            except EngineError as exc:
                issues.append(
                    {"tile": spec.key, "reason": "digest", "message": exc.message}
                )
                continue
            tile_pixels[spec.key] = record.pixels
        manifest, manifest_error = store.load_manifest()
        if manifest is None:
            issues.append(
                {
                    "tile": "*",
                    "reason": "manifest",
                    "message": manifest_error or "manifest is missing",
                }
            )
        else:
            if manifest.get("contentVersion") != self.scene.content_version():
                issues.append(
                    {
                        "tile": "*",
                        "reason": "manifest",
                        "message": "manifest content version differs from the scene",
                    }
                )
            tile_versions = manifest.get("tileVersions")
            expected_versions = {spec.key: spec.version for spec in specs}
            if tile_versions != expected_versions:
                issues.append(
                    {
                        "tile": "*",
                        "reason": "manifest",
                        "message": "manifest tile versions differ from expected dependencies",
                    }
                )

        bitmap_sha = ""
        if len(tile_pixels) == len(specs):
            canvas = assemble_tiles(
                self.scene, specs, [tile_pixels[spec.key] for spec in specs]
            )
            bitmap_sha = sha256_bytes(canvas.tobytes(order="C"))
            try:
                bitmap = read_pgm(self.scene.output_path, load_pixels=True)
            except EngineError as exc:
                issues.append(
                    {"tile": "*", "reason": "bitmap", "message": exc.message}
                )
            else:
                if (
                    bitmap.info.width != self.scene.width
                    or bitmap.info.height != self.scene.height
                    or bitmap.info.content_version != self.scene.content_version()
                ):
                    issues.append(
                        {
                            "tile": "*",
                            "reason": "bitmap",
                            "message": "bitmap dimensions or content version are stale",
                        }
                    )
                if bitmap.actual_pixel_sha256 != bitmap_sha:
                    issues.append(
                        {
                            "tile": "*",
                            "reason": "bitmap",
                            "message": "bitmap pixels differ from assembled tiles",
                        }
                    )
            if full_check:
                full_specs, full_tiles = rasterize_scene_tiles(self.scene)
                full_canvas = assemble_tiles(self.scene, full_specs, full_tiles)
                if not np.array_equal(canvas, full_canvas):
                    issues.append(
                        {
                            "tile": "*",
                            "reason": "full_check",
                            "message": "incremental assembly differs from full rasterization",
                        }
                    )

        report: dict | None = None
        report_path = self.scene.report_path
        if store.interrupted_exists():
            issues.append(
                {
                    "tile": "*",
                    "reason": "interrupted",
                    "message": "an interruption marker is still present",
                }
            )
        try:
            raw_report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            issues.append(
                {
                    "tile": "*",
                    "reason": "report",
                    "message": f"report cannot be read: {exc}",
                }
            )
        else:
            if not isinstance(raw_report, dict) or raw_report.get("schema") != SCHEMA_REPORT:
                issues.append(
                    {
                        "tile": "*",
                        "reason": "report",
                        "message": "report schema is invalid",
                    }
                )
            else:
                report = raw_report
                if report.get("contentVersion") != self.scene.content_version():
                    issues.append(
                        {
                            "tile": "*",
                            "reason": "report",
                            "message": "report content version differs from the scene",
                        }
                    )
                if (
                    report.get("width") != self.scene.width
                    or report.get("height") != self.scene.height
                    or report.get("supersampling") != self.scene.supersampling
                ):
                    issues.append(
                        {
                            "tile": "*",
                            "reason": "report",
                            "message": "report resolution or supersampling is inconsistent",
                        }
                    )
                tile_size = report.get("tileSize")
                if (
                    not isinstance(tile_size, dict)
                    or tile_size.get("width") != self.scene.tile_width
                    or tile_size.get("height") != self.scene.tile_height
                ):
                    issues.append(
                        {
                            "tile": "*",
                            "reason": "report",
                            "message": "report tile size is inconsistent",
                        }
                    )
                tiles = report.get("tiles")
                if not isinstance(tiles, dict) or tiles.get("total") != len(specs):
                    issues.append(
                        {
                            "tile": "*",
                            "reason": "report",
                            "message": "report tile counts are incomplete",
                        }
                    )
                expected_glyph_tiles = glyph_tile_counts(self.scene, specs)
                per_glyph = report.get("perGlyph")
                if not isinstance(per_glyph, dict) or set(per_glyph) != set(
                    expected_glyph_tiles
                ):
                    issues.append(
                        {
                            "tile": "*",
                            "reason": "report",
                            "message": "report per-glyph entries are inconsistent",
                        }
                    )
                else:
                    for glyph_id, expected_count in expected_glyph_tiles.items():
                        entry = per_glyph.get(glyph_id)
                        if (
                            not isinstance(entry, dict)
                            or entry.get("tileCount") != expected_count
                        ):
                            issues.append(
                                {
                                    "tile": "*",
                                    "reason": "report",
                                    "message": f"report tile count is inconsistent for {glyph_id}",
                                }
                            )
                bitmap_info = report.get("bitmap")
                if (
                    not isinstance(bitmap_info, dict)
                    or bitmap_info.get("sha256") != bitmap_sha
                ):
                    issues.append(
                        {
                            "tile": "*",
                            "reason": "report",
                            "message": "report bitmap digest is inconsistent",
                        }
                    )
                if report.get("code") == "E_DIGEST":
                    issues.append(
                        {
                            "tile": "*",
                            "reason": "reported_digest",
                            "message": "the report records a previous E_DIGEST recovery",
                        }
                    )

        result = {
            "status": "ok" if not issues else "error",
            "code": "OK" if not issues else "E_DIGEST",
            "exitCode": 0 if not issues else 5,
            "contentVersion": self.scene.content_version(),
            "tiles": len(specs),
            "bitmapSha256": bitmap_sha,
            "fullCheck": bool(full_check),
            "issues": issues,
            "reportPath": str(report_path),
        }
        return result
