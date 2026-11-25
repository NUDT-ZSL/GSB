from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np

from .cache import CacheHooks, ContentAddressedCache
from .deterministic import (
    array_digest,
    combine_digests,
    diff_heatmap,
    png_encode,
    sha256_bytes,
    sha256_json,
    to_uint8,
)
from .errors import (
    BusinessError,
    E_BLOCK_MISMATCH,
    E_TOLERANCE_EXCEEDED,
)
from .graph import (
    ControlPlane,
    RunArtifacts,
    assert_block_match,
    error_report,
    execute_block,
    execute_whole,
)
from .models import Sample, StyleParams, mask_from_payload, mask_payload
from .precision import Executor, PrecisionSpec
from .state import (
    CheckpointStore,
    SnapshotStore,
    StageGuard,
    StateDatabase,
)


def _b64_png(rgb: np.ndarray) -> str:
    return base64.b64encode(png_encode(rgb)).decode("ascii")


def _mask_visual(mask: np.ndarray, sample: Sample) -> np.ndarray:
    palette = {
        1: (230, 83, 74),
        2: (66, 190, 140),
        3: (67, 137, 226),
    }
    output = np.zeros((*mask.shape, 3), dtype=np.uint8)
    output[:] = np.array([16, 22, 31], dtype=np.uint8)
    for region in sample.regions:
        color = palette.get(region.label, (220, 220, 220))
        output[mask == region.label] = np.array(color, dtype=np.uint8)
    return output


def _atomic_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _erode_region(mask: np.ndarray, label: int) -> np.ndarray:
    region = mask == label
    padded = np.pad(region, 1, mode="constant", constant_values=False)
    eroded = (
        region
        & padded[:-2, 1:-1]
        & padded[2:, 1:-1]
        & padded[1:-1, :-2]
        & padded[1:-1, 2:]
    )
    if int(np.count_nonzero(eroded)) < 12:
        raise BusinessError(
            "E_MASK_EDIT_INVALID",
            "region mask would become too small",
            {"label": label, "remaining_pixels": int(np.count_nonzero(eroded))},
        )
    updated = np.asarray(mask, dtype=np.uint8).copy()
    updated[region] = 0
    updated[eroded] = np.uint8(label)
    return updated


class ImageDiagService:
    def __init__(
        self,
        workspace: str | Path = "data/runtime",
        sample_dir: str | Path = "data/sample",
    ) -> None:
        self.workspace = Path(workspace)
        self.sample_dir = Path(sample_dir)
        self.cache_root = self.workspace / "cache"
        self.locks_dir = self.workspace / "locks"
        self.runs_dir = self.workspace / "runs"
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.cache_root.mkdir(parents=True, exist_ok=True)
        self.locks_dir.mkdir(parents=True, exist_ok=True)
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self.canonical_sample = Sample.load(self.sample_dir)
        self.database = StateDatabase(self.workspace / "state.db")
        self.snapshots = SnapshotStore(self.database)
        self.checkpoints = CheckpointStore(self.database)
        self.cache = ContentAddressedCache(self.cache_root)
        self._lock = threading.RLock()
        self._control_cache: dict[str, ControlPlane] = {}
        self._error_log: list[dict[str, Any]] = []
        self._last_execution: tuple[ControlPlane, RunArtifacts, RunArtifacts] | None = None
        self.sample = self._load_current_sample()
        self._ensure_sample_snapshots()
        self._save_runtime(self.sample)

    # ------------------------------------------------------------------
    # Runtime and snapshots
    # ------------------------------------------------------------------
    def _load_current_sample(self) -> Sample:
        base = self.canonical_sample.base.copy()
        mask = self.canonical_sample.mask.copy()
        style = self.canonical_sample.style
        runtime_mask = self.workspace / "mask.npy"
        runtime_style = self.workspace / "style.json"
        if runtime_mask.exists():
            mask = np.load(runtime_mask, allow_pickle=False)
        else:
            current = self.snapshots.current("mask")
            if current is not None:
                mask = mask_from_payload(current["payload"]["mask"])
        if runtime_style.exists():
            style = StyleParams.from_dict(
                json.loads(runtime_style.read_text(encoding="utf-8"))
            )
        else:
            current = self.snapshots.current("style")
            if current is not None:
                style = StyleParams.from_dict(current["payload"]["style"])
        sample = Sample(
            base=base,
            mask=np.asarray(mask, dtype=np.uint8),
            regions=self.canonical_sample.regions,
            style=style,
            seed=self.canonical_sample.seed,
            source=self.canonical_sample.source,
            metadata=dict(self.canonical_sample.metadata),
        )
        sample.validate()
        return sample

    def _save_runtime(self, sample: Sample) -> None:
        self.workspace.mkdir(parents=True, exist_ok=True)
        np.save(self.workspace / "base.npy", sample.base, allow_pickle=False)
        np.save(self.workspace / "mask.npy", sample.mask, allow_pickle=False)
        _atomic_bytes(
            self.workspace / "style.json",
            (json.dumps(sample.style.as_dict(), indent=2, sort_keys=True) + "\n").encode(
                "utf-8"
            ),
        )
        _atomic_bytes(
            self.workspace / "regions.json",
            (
                json.dumps(
                    [region.as_dict() for region in sample.regions],
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            ).encode("utf-8"),
        )

    def _mask_snapshot_payload(self, sample: Sample) -> dict[str, Any]:
        return {
            "mask": mask_payload(sample.mask),
            "region_set": [
                {"id": region.id, "label": region.label} for region in sample.regions
            ],
        }

    def _style_snapshot_payload(self, sample: Sample) -> dict[str, Any]:
        return {
            "style": sample.style.as_dict(),
            "style_sha256": sample.style_hash,
        }

    def _ensure_snapshot(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        current = self.snapshots.current(kind)
        checksum = sha256_json(payload)
        if current is not None and current["checksum"] == checksum:
            return current
        return self.snapshots.commit(
            kind,
            payload,
            expected_version=current["version"] if current else None,
            expected_checksum=current["checksum"] if current else None,
        )

    def _ensure_sample_snapshots(self) -> None:
        self._ensure_snapshot("mask", self._mask_snapshot_payload(self.sample))
        self._ensure_snapshot("style", self._style_snapshot_payload(self.sample))

    def _set_sample(self, sample: Sample) -> None:
        sample.validate()
        self.sample = sample
        self._save_runtime(sample)

    def snapshot_state(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for kind in ("mask", "style", "result"):
            current = self.snapshots.current(kind)
            if current is None:
                result[kind] = None
                continue
            payload = current["payload"]
            summary: dict[str, Any] = {}
            if kind == "mask":
                summary = {
                    "mask_sha256": payload["mask"]["sha256"],
                    "shape": payload["mask"]["shape"],
                }
            elif kind == "style":
                summary = {
                    "style_sha256": payload["style_sha256"],
                    "style_version": payload["style"]["version"],
                }
            elif kind == "result":
                summary = {
                    "final_sha256": payload.get("final_sha256"),
                    "precision": payload.get("precision"),
                    "path": payload.get("path"),
                    "png_sha256": payload.get("png_sha256"),
                }
            result[kind] = {
                "version": current["version"],
                "checksum": current["checksum"],
                **summary,
            }
        return result

    def state_payload(self) -> dict[str, Any]:
        return {
            "ok": True,
            "sample": {
                "seed": self.sample.seed,
                "width": self.sample.width,
                "height": self.sample.height,
                "base_sha256": self.sample.base_hash,
                "mask_sha256": self.sample.mask_hash,
                "style_sha256": self.sample.style_hash,
                "regions": [region.as_dict() for region in self.sample.regions],
                "style": self.sample.style.as_dict(),
            },
            "snapshots": self.snapshot_state(),
            "snapshot_history": {
                "mask": [
                    {"version": item["version"], "checksum": item["checksum"]}
                    for item in self.snapshots.history("mask", limit=6)
                ],
                "style": [
                    {"version": item["version"], "checksum": item["checksum"]}
                    for item in self.snapshots.history("style", limit=6)
                ],
            },
            "cache": self.cache.summary(),
            "checkpoints": self.checkpoints.counts(),
            "error_log": self._error_log[-12:],
            "last_run": self._last_execution_summary(),
        }

    def _last_execution_summary(self) -> dict[str, Any] | None:
        if self._last_execution is None:
            return None
        _, whole, block = self._last_execution
        return {
            "whole_hash": whole.final_hash,
            "block_hash": block.final_hash,
            "bitwise_equal": array_digest(whole.fused) == array_digest(block.fused),
        }

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------
    def _effective_sample(
        self,
        *,
        policy: str | None,
        threshold_tolerance: float | None,
        error_tolerance: float | None,
    ) -> Sample:
        style = self.sample.style
        if (
            policy is not None
            or threshold_tolerance is not None
            or error_tolerance is not None
        ):
            style = style.revised(
                policy=policy,
                threshold_tolerance=threshold_tolerance,
                error_tolerance=error_tolerance,
            )
        return Sample(
            base=self.sample.base.copy(),
            mask=self.sample.mask.copy(),
            regions=self.sample.regions,
            style=style,
            seed=self.sample.seed,
            source=self.sample.source,
            metadata=dict(self.sample.metadata),
        )

    def _control_for(self, sample: Sample) -> ControlPlane:
        key = combine_digests(
            sample.base_hash, sample.mask_hash, sample.style_hash
        )
        with self._lock:
            control = self._control_cache.get(key)
            if control is None:
                control = ControlPlane(sample, sample.style)
                self._control_cache[key] = control
                if len(self._control_cache) > 8:
                    oldest = next(iter(self._control_cache))
                    if oldest != key:
                        self._control_cache.pop(oldest, None)
            return control

    def run_key_for(
        self,
        sample: Sample,
        *,
        precision: str,
        focus_path: str,
        tolerance: float,
    ) -> str:
        return combine_digests(
            sample.base_hash,
            sample.mask_hash,
            sample.style_hash,
            precision,
            focus_path,
            sha256_json(
                {
                    "threshold_policy": sample.style.threshold_policy,
                    "threshold_tolerance": sample.style.threshold_tolerance,
                    "boundary_threshold": sample.style.boundary_threshold,
                    "style_threshold": sample.style.style_threshold,
                    "tolerance": float(tolerance),
                }
            ),
        )

    def _assert_stage_match(
        self,
        whole: RunArtifacts,
        block: RunArtifacts,
    ) -> dict[str, str]:
        whole_hashes = {
            record["stage"]: record["output_hash"] for record in whole.stage_records
        }
        block_hashes = {
            record["stage"]: record["output_hash"] for record in block.stage_records
        }
        mismatch = {
            stage: {"whole": whole_hashes.get(stage), "block": block_hashes.get(stage)}
            for stage in whole_hashes
            if whole_hashes.get(stage) != block_hashes.get(stage)
        }
        if mismatch:
            raise BusinessError(
                E_BLOCK_MISMATCH,
                "block and whole paths produced different intermediate stage artifacts",
                {"stage_mismatches": mismatch},
                http_status=409,
            )
        return block_hashes

    def _execute_pair(
        self,
        sample: Sample,
        precision: str,
        *,
        hooks: CacheHooks | None = None,
    ) -> tuple[ControlPlane, RunArtifacts, RunArtifacts]:
        control = self._control_for(sample)
        executor = Executor(
            PrecisionSpec.create(
                precision,
                sample.style.fixed_scale,
                sample.style.fixed_rounding,
            )
        )
        if precision == "float64":
            whole = control.reference
        else:
            whole = execute_whole(control, executor)
        block = execute_block(control, executor, stats=whole.stats, hooks=hooks)
        assert_block_match(whole, block, control)
        self._assert_stage_match(whole, block)
        self._last_execution = (control, whole, block)
        return control, whole, block

    def run_compare(
        self,
        *,
        precision: str = "float64",
        focus_path: str = "block",
        hooks: CacheHooks | None = None,
        policy: str | None = None,
        threshold_tolerance: float | None = None,
        error_tolerance: float | None = None,
        commit_result: bool = True,
    ) -> dict[str, Any]:
        if focus_path not in ("whole", "block"):
            raise BusinessError(
                "E_BAD_REQUEST",
                "focus_path must be whole or block",
                {"focus_path": focus_path},
            )
        sample = self._effective_sample(
            policy=policy,
            threshold_tolerance=threshold_tolerance,
            error_tolerance=error_tolerance,
        )
        tolerance = (
            float(error_tolerance)
            if error_tolerance is not None
            else sample.style.error_tolerance
        )
        run_key = self.run_key_for(
            sample,
            precision=precision,
            focus_path=focus_path,
            tolerance=tolerance,
        )
        try:
            control, whole, block = self._execute_pair(
                sample, precision, hooks=hooks
            )
        except BusinessError as exc:
            self._record_error(exc)
            raise
        candidate = block if focus_path == "block" else whole
        diagnostics = error_report(control.reference, candidate, tolerance=tolerance)
        tolerance_error = None
        if not diagnostics["within_tolerance"]:
            tolerance_error = {
                "code": E_TOLERANCE_EXCEEDED,
                "message": "error interval exceeds the declared tolerance",
                "details": {
                    "tolerance": tolerance,
                    "final_max_abs": diagnostics["cascade"][-1]["abs_max"],
                    "dominant_source": diagnostics["dominant_source"],
                    "cascade": diagnostics["cascade"],
                },
            }
            self._record_error(
                BusinessError(
                    E_TOLERANCE_EXCEEDED,
                    tolerance_error["message"],
                    tolerance_error["details"],
                    http_status=422,
                )
            )
        images = self._build_images(sample, control, whole, block, candidate)
        result_payload = {
            "final_sha256": candidate.final_hash,
            "whole_sha256": whole.final_hash,
            "block_sha256": block.final_hash,
            "precision": precision,
            "path": focus_path,
            "png_sha256": sha256_bytes(
                png_encode(
                    to_uint8(
                        block.fused if focus_path == "block" else whole.fused
                    )
                )
            ),
            "png_base64": images["focus_png"],
        }
        if commit_result:
            current = self.snapshots.current("result")
            snapshot = self.snapshots.commit(
                "result",
                result_payload,
                expected_version=current["version"] if current else None,
                expected_checksum=current["checksum"] if current else None,
            )
        else:
            snapshot = None
        response = {
            "ok": True,
            "run_key": run_key,
            "precision": precision,
            "focus_path": focus_path,
            "sample": {
                "base_sha256": sample.base_hash,
                "mask_sha256": sample.mask_hash,
                "style_sha256": sample.style_hash,
                "style_version": sample.style.version,
            },
            "whole": {
                "final_sha256": whole.final_hash,
                "stage_records": whole.stage_records,
            },
            "block": {
                "final_sha256": block.final_hash,
                "stage_records": block.stage_records,
                "region_events": block.region_events,
            },
            "block_match": {
                "bitwise_equal": True,
                "max_abs_delta": 0.0,
                "compared_pixels": int(sample.height * sample.width),
            },
            "diagnostics": diagnostics,
            "boundary_spectrum": self._boundary_spectrum(
                control, whole, block, control.reference
            ),
            "cache_events": self._cache_event_summary(block.region_events),
            "cache": hooks.cache.summary() if hooks is not None else self.cache.summary(),
            "snapshot": (
                {
                    "kind": snapshot["kind"],
                    "version": snapshot["version"],
                    "checksum": snapshot["checksum"],
                }
                if snapshot is not None
                else None
            ),
            "snapshots": self.snapshot_state(),
            "images": {key: value for key, value in images.items() if key != "focus_png"},
            "error": tolerance_error,
        }
        self._write_run_artifacts(
            run_key, response, whole, block, sample
        )
        return response

    def _build_images(
        self,
        sample: Sample,
        control: ControlPlane,
        whole: RunArtifacts,
        block: RunArtifacts,
        candidate: RunArtifacts,
    ) -> dict[str, str]:
        base_png = png_encode(to_uint8(sample.base))
        mask_png = png_encode(_mask_visual(sample.mask, sample))
        whole_png = png_encode(to_uint8(whole.fused))
        block_png = png_encode(to_uint8(block.fused))
        block_diff = diff_heatmap(whole.fused, block.fused)
        error_diff = diff_heatmap(candidate.fused, control.reference.fused)
        return {
            "base": _b64_png(to_uint8(sample.base)),
            "mask": _b64_png(_mask_visual(sample.mask, sample)),
            "whole_png": base64.b64encode(whole_png).decode("ascii"),
            "block_png": base64.b64encode(block_png).decode("ascii"),
            "diff": _b64_png(block_diff),
            "error": _b64_png(error_diff),
            "focus_png": base64.b64encode(
                png_encode(to_uint8(candidate.fused))
            ).decode("ascii"),
            "base_png": base64.b64encode(base_png).decode("ascii"),
            "mask_png": base64.b64encode(mask_png).decode("ascii"),
        }

    def _boundary_spectrum(
        self,
        control: ControlPlane,
        whole: RunArtifacts,
        block: RunArtifacts,
        reference: RunArtifacts,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for runtime in control.runtimes:
            mask = runtime.mask
            context_delta = np.abs(block.context[mask] - reference.context[mask])
            fused_delta = np.abs(block.fused[mask] - reference.fused[mask])
            block_whole_delta = np.abs(block.fused[mask] - whole.fused[mask])
            rows.append(
                {
                    "region": runtime.id,
                    "area": runtime.area,
                    "perimeter": runtime.perimeter,
                    "boundary_max_abs": (
                        float(np.max(context_delta)) if context_delta.size else 0.0
                    ),
                    "final_max_abs": (
                        float(np.max(fused_delta)) if fused_delta.size else 0.0
                    ),
                    "block_vs_whole_max_abs": (
                        float(np.max(block_whole_delta))
                        if block_whole_delta.size
                        else 0.0
                    ),
                }
            )
        return rows

    def _cache_event_summary(
        self, events: list[dict[str, Any]]
    ) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for event in events:
            status = str(event.get("status", "unknown"))
            counts[status] = counts.get(status, 0) + 1
        return {
            "total": len(events),
            "by_status": counts,
            "reused": sum(
                count
                for status, count in counts.items()
                if status in ("cache_hit", "resumed_checkpoint")
            ),
            "recomputed": counts.get("recomputed", 0),
        }

    def _write_run_artifacts(
        self,
        run_key: str,
        response: dict[str, Any],
        whole: RunArtifacts,
        block: RunArtifacts,
        sample: Sample,
    ) -> None:
        directory = self.runs_dir / run_key[:20]
        directory.mkdir(parents=True, exist_ok=True)
        document = {
            key: value
            for key, value in response.items()
            if key not in ("images", "snapshots")
        }
        _atomic_bytes(
            directory / "run.json",
            (json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
                "utf-8"
            ),
        )
        _atomic_bytes(directory / "whole.png", png_encode(to_uint8(whole.fused)))
        _atomic_bytes(directory / "block.png", png_encode(to_uint8(block.fused)))
        _atomic_bytes(directory / "base.png", png_encode(to_uint8(sample.base)))
        _atomic_bytes(
            directory / "mask.png", png_encode(_mask_visual(sample.mask, sample))
        )
        _atomic_bytes(
            directory / "block_whole_diff.png",
            png_encode(diff_heatmap(whole.fused, block.fused)),
        )

    def _record_error(self, error: BusinessError) -> None:
        with self._lock:
            self._error_log.append(
                {
                    "code": error.code,
                    "message": error.message,
                    "details": error.details,
                }
            )
            self._error_log = self._error_log[-24:]

    # ------------------------------------------------------------------
    # Demonstrations
    # ------------------------------------------------------------------
    def demo_incremental(self) -> dict[str, Any]:
        with StageGuard(self.locks_dir, "region-incremental"):
            baseline_sample = self.sample
            baseline_key = self.run_key_for(
                baseline_sample,
                precision="float64",
                focus_path="block",
                tolerance=baseline_sample.style.error_tolerance,
            )
            self.checkpoints.reset(baseline_key)
            baseline_hooks = CacheHooks(
                self.cache,
                self.checkpoints,
                run_key=baseline_key,
            )
            baseline = self.run_compare(
                precision="float64",
                focus_path="block",
                hooks=baseline_hooks,
            )

            updated_mask = _erode_region(self.sample.mask, 2)
            updated_sample = self.sample.with_mask(updated_mask)
            self._set_sample(updated_sample)
            mask_snapshot = self._ensure_snapshot(
                "mask", self._mask_snapshot_payload(updated_sample)
            )
            incremental_key = self.run_key_for(
                updated_sample,
                precision="float64",
                focus_path="block",
                tolerance=updated_sample.style.error_tolerance,
            )
            self.checkpoints.reset(incremental_key)
            incremental_hooks = CacheHooks(
                self.cache,
                self.checkpoints,
                run_key=incremental_key,
            )
            incremental = self.run_compare(
                precision="float64",
                focus_path="block",
                hooks=incremental_hooks,
            )
            assert self._last_execution is not None
            incremental_whole = self._last_execution[1]
            incremental_block = self._last_execution[2]
            _, full_whole, full_block = self._execute_pair(
                updated_sample, "float64", hooks=None
            )
            incremental_equal_full = bool(
                np.array_equal(incremental_block.fused, full_block.fused)
                and np.array_equal(incremental_whole.fused, full_whole.fused)
            )
            events = incremental["block"]["region_events"]
            rebuilt = sorted(
                {
                    event["region"]
                    for event in events
                    if event["status"] == "recomputed"
                }
            )
            reused = sorted(
                {
                    event["region"]
                    for event in events
                    if event["status"] in ("cache_hit", "resumed_checkpoint")
                }
            )
            return {
                "ok": True,
                "scenario": "incremental",
                "baseline_final_sha256": baseline["block"]["final_sha256"],
                "incremental_final_sha256": incremental["block"]["final_sha256"],
                "full_recompute_final_sha256": full_block.final_hash,
                "incremental_equal_full": incremental_equal_full,
                "edited_region": "R2",
                "mask_snapshot": {
                    "version": mask_snapshot["version"],
                    "checksum": mask_snapshot["checksum"],
                },
                "recomputed_regions": rebuilt,
                "reused_regions": reused,
                "cache_events": self._cache_event_summary(events),
                "cache": incremental_hooks.status(),
                "snapshots": self.snapshot_state(),
                "incremental_run": incremental,
            }

    def revise_style(self, **changes: Any) -> dict[str, Any]:
        current = self.sample.style
        updated_values = dict(changes)
        updated_values.setdefault("version", current.version + 1)
        updated = replace(current, **updated_values)
        updated.validate()
        sample = self.sample.with_mask(self.sample.mask, updated)
        self._set_sample(sample)
        snapshot = self._ensure_snapshot(
            "style", self._style_snapshot_payload(sample)
        )
        return {
            "ok": True,
            "scenario": "style_revision",
            "style": updated.as_dict(),
            "style_sha256": sample.style_hash,
            "snapshot": {
                "version": snapshot["version"],
                "checksum": snapshot["checksum"],
            },
        }

    def demo_style_revision(self) -> dict[str, Any]:
        with StageGuard(self.locks_dir, "style-revision"):
            baseline_sample = self.sample
            baseline_key = self.run_key_for(
                baseline_sample,
                precision="float64",
                focus_path="block",
                tolerance=baseline_sample.style.error_tolerance,
            )
            self.checkpoints.reset(baseline_key)
            baseline_hooks = CacheHooks(
                self.cache,
                self.checkpoints,
                run_key=baseline_key,
            )
            baseline = self.run_compare(
                precision="float64",
                focus_path="block",
                hooks=baseline_hooks,
            )
            next_strength = min(
                0.95, round(self.sample.style.strength + 0.01, 4)
            )
            revision = self.revise_style(strength=next_strength)
            revised_sample = self.sample
            revised_key = self.run_key_for(
                revised_sample,
                precision="float64",
                focus_path="block",
                tolerance=revised_sample.style.error_tolerance,
            )
            self.checkpoints.reset(revised_key)
            revised_hooks = CacheHooks(
                self.cache,
                self.checkpoints,
                run_key=revised_key,
            )
            revised = self.run_compare(
                precision="float64",
                focus_path="block",
                hooks=revised_hooks,
            )
            assert self._last_execution is not None
            revised_whole = self._last_execution[1]
            revised_block = self._last_execution[2]
            _, full_whole, full_block = self._execute_pair(
                revised_sample, "float64", hooks=None
            )
            equal_full = bool(
                np.array_equal(revised_block.fused, full_block.fused)
                and np.array_equal(revised_whole.fused, full_whole.fused)
            )
            events = revised["block"]["region_events"]
            by_stage: dict[str, dict[str, int]] = {}
            for event in events:
                stage = event["stage"]
                status = event["status"]
                by_stage.setdefault(stage, {})
                by_stage[stage][status] = by_stage[stage].get(status, 0) + 1
            return {
                "ok": True,
                "scenario": "style_revision",
                "baseline_final_sha256": baseline["block"]["final_sha256"],
                "revised_final_sha256": revised["block"]["final_sha256"],
                "full_recompute_final_sha256": full_block.final_hash,
                "revision_equal_full": equal_full,
                "style_snapshot": revision["snapshot"],
                "cache_events": self._cache_event_summary(events),
                "events_by_stage": by_stage,
                "upstream_reused": all(
                    by_stage.get(stage, {}).get("cache_hit", 0) == 3
                    for stage in ("parse", "boundary", "completion")
                ),
                "downstream_recomputed": all(
                    by_stage.get(stage, {}).get("recomputed", 0) == 3
                    for stage in ("style", "fusion")
                ),
                "cache": revised_hooks.status(),
                "revised_run": revised,
            }

    def demo_rollback(self) -> dict[str, Any]:
        with StageGuard(self.locks_dir, "snapshot-rollback"):
            current = self.snapshots.current("mask")
            if current is None:
                self._ensure_sample_snapshots()
                current = self.snapshots.current("mask")
            assert current is not None
            if current["version"] < 2:
                current = self.snapshots.commit(
                    "mask",
                    current["payload"],
                    expected_version=current["version"],
                    expected_checksum=current["checksum"],
                )
            history = self.snapshots.history("mask", limit=20)
            older = next(
                item for item in reversed(history) if item["version"] < current["version"]
            )
            head_before = self.snapshots.current("mask")
            error: BusinessError | None = None
            try:
                self.snapshots.commit(
                    "mask",
                    current["payload"],
                    expected_version=older["version"],
                )
            except BusinessError as exc:
                error = exc
                self._record_error(exc)
            head_after = self.snapshots.current("mask")
            assert error is not None
            assert head_before is not None and head_after is not None
            return {
                "ok": True,
                "scenario": "rollback",
                "error": {
                    "code": error.code,
                    "message": error.message,
                    "details": error.details,
                },
                "head_unchanged": (
                    head_before["version"] == head_after["version"]
                    and head_before["checksum"] == head_after["checksum"]
                ),
                "current_version": head_after["version"],
                "attempted_version": older["version"],
                "snapshots": self.snapshot_state(),
            }

    def demo_busy(self) -> dict[str, Any]:
        with StageGuard(self.locks_dir, "region-incremental"):
            root = Path(__file__).resolve().parent.parent
            environment = dict(os.environ)
            environment["PYTHONPATH"] = str(root)
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "image_diag",
                    "lock-probe",
                    "--workspace",
                    str(self.workspace),
                    "--stage",
                    "region-incremental",
                ],
                cwd=str(root),
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            try:
                payload = json.loads(completed.stdout.strip())
            except json.JSONDecodeError as exc:
                raise BusinessError(
                    "E_BAD_REQUEST",
                    "unable to read lock-probe output",
                    {"stdout": completed.stdout, "stderr": completed.stderr},
                ) from exc
        if payload.get("ok"):
            raise BusinessError(
                "E_BAD_REQUEST",
                "lock-probe unexpectedly acquired a held stage",
                payload,
            )
        return {
            "ok": True,
            "scenario": "stage_busy",
            "error": payload["error"],
            "cross_process": True,
            "probe_exit_code": completed.returncode,
        }

    def demo_recovery(self, *, fail_after: int = 6) -> dict[str, Any]:
        sample = self.sample
        run_key = self.run_key_for(
            sample,
            precision="float64",
            focus_path="block",
            tolerance=sample.style.error_tolerance,
        )
        self.checkpoints.reset(run_key)
        root = Path(__file__).resolve().parent.parent
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(root)
        child = subprocess.run(
            [
                sys.executable,
                "-m",
                "image_diag",
                "worker-run",
                "--workspace",
                str(self.workspace),
                "--sample-dir",
                str(self.sample_dir),
                "--precision",
                "float64",
                "--fail-after",
                str(fail_after),
                "--force-recompute",
            ],
            cwd=str(root),
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        committed_before_resume = self.checkpoints.load(run_key)
        resume_hooks = CacheHooks(
            self.cache,
            self.checkpoints,
            run_key=run_key,
        )
        recovered = self.run_compare(
            precision="float64",
            focus_path="block",
            hooks=resume_hooks,
        )
        assert self._last_execution is not None
        recovered_whole = self._last_execution[1]
        recovered_block = self._last_execution[2]
        events = recovered["block"]["region_events"]
        resumed = [
            event for event in events if event["status"] == "resumed_checkpoint"
        ]
        recomputed = [
            event for event in events if event["status"] == "recomputed"
        ]
        _, full_whole, full_block = self._execute_pair(
            sample, "float64", hooks=None
        )
        recovery_equal_full = bool(
            np.array_equal(recovered_block.fused, full_block.fused)
            and np.array_equal(recovered_whole.fused, full_whole.fused)
        )
        return {
            "ok": True,
            "scenario": "recovery",
            "run_key": run_key,
            "terminated_process_return_code": child.returncode,
            "checkpoint_count_before_resume": len(committed_before_resume),
            "resumed_checkpoint_events": len(resumed),
            "recomputed_after_resume_events": len(recomputed),
            "resumed_stages": [
                {"stage": event["stage"], "region": event["region"]}
                for event in resumed
            ],
            "recovery_equal_full": recovery_equal_full,
            "final_sha256": recovered["block"]["final_sha256"],
            "cache": resume_hooks.status(),
            "stderr_tail": child.stderr[-800:],
            "recovered_run": recovered,
        }

    def reset_runtime_to_sample(self) -> dict[str, Any]:
        self._set_sample(self.canonical_sample)
        self._ensure_sample_snapshots()
        return {
            "ok": True,
            "mask_sha256": self.sample.mask_hash,
            "style_sha256": self.sample.style_hash,
            "snapshots": self.snapshot_state(),
        }
