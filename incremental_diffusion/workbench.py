"""Incremental execution engine and query API."""

from __future__ import annotations

import io
import os
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import numpy as np
from PIL import Image

from .artifacts import Artifact, save_artifact
from .canonical import array_fingerprint, atomic_write_bytes
from .config import LoadedConfig, load_config
from .errors import (
    E_STAGE_FAILURE,
    WorkbenchError,
)
from .pipeline import StageSpec, build_stage_specs, compute_stage
from .report import (
    build_report,
    pixel_diff_summary,
    render_dependency_graph,
    write_report,
)
from .snapshots import (
    ARTIFACTS_DIR,
    SnapshotInfo,
    SnapshotStore,
    artifact_relative_path,
    refresh_manifest_fingerprint,
    snapshot_conflict,
)


FAIL_STAGE_ENV = "IMGDIFF_FAIL_STAGE"
INTERRUPT_AFTER_STAGE_ENV = "IMGDIFF_INTERRUPT_AFTER_STAGE"
CRASH_AFTER_STAGE_ENV = "IMGDIFF_CRASH_AFTER_STAGE"
DELAY_STAGE_ENV = "IMGDIFF_DELAY_STAGE"
STAGE_DELAY_SECONDS_ENV = "IMGDIFF_STAGE_DELAY_SECONDS"


@dataclass
class PlanResult:
    config: LoadedConfig
    specs: list[StageSpec]
    reuse_stages: list[str]
    recompute_stages: list[str]
    statuses: dict[str, str]
    candidates: dict[str, Artifact]
    resolved: Any

    @property
    def dependencies(self) -> dict[str, list[str]]:
        return {spec.stage_id: list(spec.dependencies) for spec in self.specs}


@dataclass
class RunOutcome:
    status: str
    exit_code: int
    entry: dict[str, Any]
    report: dict[str, Any]
    final_pixels: np.ndarray | None = None
    snapshot_path: Path | None = None


class SignalController:
    def __init__(self) -> None:
        self.requested = False
        self.signal_name: str | None = None
        self._old_handlers: dict[int, Any] = {}

    def install(self) -> None:
        for signal_number in (signal.SIGINT, signal.SIGTERM):
            try:
                self._old_handlers[signal_number] = signal.getsignal(signal_number)
                signal.signal(signal_number, self._handle)
            except (ValueError, OSError):
                self._old_handlers.pop(signal_number, None)

    def restore(self) -> None:
        for signal_number, handler in self._old_handlers.items():
            try:
                signal.signal(signal_number, handler)
            except (ValueError, OSError):
                pass
        self._old_handlers.clear()

    def _handle(self, signum: int, _frame: Any) -> None:
        self.requested = True
        try:
            self.signal_name = signal.Signals(signum).name
        except ValueError:
            self.signal_name = str(signum)

    def apply_test_hook_after(self, stage_id: str) -> None:
        if os.environ.get(INTERRUPT_AFTER_STAGE_ENV) == stage_id:
            self.requested = True
            self.signal_name = "SIGINT(test-hook)"

    def crash_hook(self, stage_id: str) -> None:
        if os.environ.get(CRASH_AFTER_STAGE_ENV) == stage_id:
            os._exit(137)

    def fail_hook(self, stage_id: str) -> None:
        if os.environ.get(FAIL_STAGE_ENV) == stage_id:
            raise RuntimeError(f"injected stage failure at {stage_id}")

    def delay_hook(self, stage_id: str) -> None:
        if os.environ.get(DELAY_STAGE_ENV) != stage_id:
            return
        value = os.environ.get(STAGE_DELAY_SECONDS_ENV)
        if value is None:
            return
        try:
            seconds = float(value)
        except ValueError:
            return
        if seconds > 0:
            time.sleep(min(seconds, 30.0))


def _write_png_atomic(path: Path, pixels: np.ndarray) -> None:
    buffer = io.BytesIO()
    Image.fromarray(np.asarray(pixels, dtype=np.uint8), mode="RGB").save(
        buffer, format="PNG", optimize=False
    )
    atomic_write_bytes(path, buffer.getvalue())


def _write_npy_atomic(path: Path, array: np.ndarray) -> None:
    buffer = io.BytesIO()
    np.save(buffer, np.ascontiguousarray(array), allow_pickle=False)
    atomic_write_bytes(path, buffer.getvalue())


class Workbench:
    def __init__(self, workspace: Path | str) -> None:
        self.workspace = Path(workspace).resolve()
        self.store = SnapshotStore(self.workspace)

    @staticmethod
    def _record_dependencies(spec: StageSpec, hit_fingerprints: Mapping[str, str]) -> dict[str, str]:
        return {dependency: hit_fingerprints[dependency] for dependency in spec.dependencies}

    def _build_plan(self, config: LoadedConfig) -> PlanResult:
        specs = build_stage_specs(config)
        resolved = self.store.resolve(
            target_version=config.snapshot_version,
            target_config_hash=config.config_hash,
        )

        sources: list[SnapshotInfo] = []
        if resolved.target_complete is not None:
            sources.append(resolved.target_complete)
        elif resolved.current is not None and resolved.current.version == config.snapshot_version:
            # A published snapshot for the requested version is authoritative;
            # never let a stale same-version staging directory shadow it.
            sources.append(resolved.current)
        else:
            if resolved.staging is not None:
                sources.append(resolved.staging)
            if resolved.current is not None:
                sources.append(resolved.current)

        hit_fingerprints: dict[str, str] = {}
        statuses: dict[str, str] = {}
        candidates: dict[str, Artifact] = {}
        reuse_stages: list[str] = []
        recompute_stages: list[str] = []

        for spec in specs:
            dependencies_hit = all(dependency in hit_fingerprints for dependency in spec.dependencies)
            expected_dependencies = (
                self._record_dependencies(spec, hit_fingerprints) if dependencies_hit else {}
            )
            candidate: Artifact | None = None
            if dependencies_hit:
                for source in sources:
                    artifact = source.artifacts.get(spec.stage_id)
                    record = source.manifest.get("stages", {}).get(spec.stage_id)
                    if artifact is None or not isinstance(record, dict):
                        continue
                    if record.get("parameter_fingerprint") != spec.parameter_fingerprint:
                        continue
                    if record.get("dependency_fingerprints") != expected_dependencies:
                        continue
                    if artifact.meta.get("kind") != spec.artifact_kind:
                        continue
                    candidate = artifact
                    break
            if candidate is not None:
                reuse_stages.append(spec.stage_id)
                statuses[spec.stage_id] = "reused"
                candidates[spec.stage_id] = candidate
                hit_fingerprints[spec.stage_id] = candidate.content_fingerprint
            else:
                recompute_stages.append(spec.stage_id)
                statuses[spec.stage_id] = "invalidated"

        same_version_published = (
            (resolved.current is not None and resolved.current.version == config.snapshot_version)
            or resolved.target_complete is not None
        )
        if same_version_published and recompute_stages:
            raise snapshot_conflict(
                "the requested snapshot version already exists with different stage content",
                snapshot_version=config.snapshot_version,
                invalidated_stages=recompute_stages,
                reused_stages=reuse_stages,
            )

        return PlanResult(
            config=config,
            specs=specs,
            reuse_stages=reuse_stages,
            recompute_stages=recompute_stages,
            statuses=statuses,
            candidates=candidates,
            resolved=resolved,
        )

    def query(self, config_path: Path | str) -> dict[str, Any]:
        config = load_config(config_path)
        plan = self._build_plan(config)
        pipeline = [spec.stage_id for spec in plan.specs]
        union = set(plan.reuse_stages).union(plan.recompute_stages)
        duplicate_count = len(plan.reuse_stages) + len(plan.recompute_stages) - len(union)
        coverage_ok = union == set(pipeline) and duplicate_count == 0
        return {
            "snapshot_version": config.snapshot_version,
            "config_hash": config.config_hash,
            "pipeline": pipeline,
            "recompute_stages": list(plan.recompute_stages),
            "reuse_stages": list(plan.reuse_stages),
            "invalidated_stages": list(plan.recompute_stages),
            "hit_stages": list(plan.reuse_stages),
            "coverage_ok": coverage_ok,
            "no_duplicates": duplicate_count == 0,
            "current_snapshot_version": (
                plan.resolved.current.version if plan.resolved.current is not None else None
            ),
            "recoverable_staging": plan.resolved.staging is not None,
            "dependencies": plan.dependencies,
            "business_code": None,
        }

    def _previous_pixels(self, resolved: Any) -> np.ndarray | None:
        current = resolved.current
        if current is None:
            return None
        artifact = current.artifacts.get("decode")
        return None if artifact is None else np.asarray(artifact.data, dtype=np.uint8)

    def _entry(
        self,
        *,
        config: LoadedConfig,
        plan: PlanResult,
        statuses: dict[str, str],
        timings_ms: dict[str, float],
        compute_times_ms: dict[str, float],
        elapsed_ms: float,
        completed_stages: list[str],
        final_info: SnapshotInfo | None,
        final_pixels: np.ndarray | None,
        previous_pixels: np.ndarray | None,
        snapshot_path: Path | None,
        snapshot_published: bool,
        status: str,
        business_code: str | None = None,
        error: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        dependencies = plan.dependencies
        stage_status = {spec.stage_id: statuses.get(spec.stage_id, "invalidated") for spec in plan.specs}
        pixel_diff = (
            pixel_diff_summary(previous_pixels, final_pixels)
            if final_pixels is not None
            else {
                "available": False,
                "identical": None,
                "reason": "no_completed_final_image",
            }
        )
        final_image_sha = (
            array_fingerprint(final_pixels) if final_pixels is not None else None
        )
        entry: dict[str, Any] = {
            "config_path": str(config.path),
            "snapshot_version": config.snapshot_version,
            "config_hash": config.config_hash,
            "pipeline": [spec.stage_id for spec in plan.specs],
            "dependencies": dependencies,
            "invalidated_stages": list(plan.recompute_stages),
            "reused_stages": list(plan.reuse_stages),
            "recompute_stages": list(plan.recompute_stages),
            "hit_stages": list(plan.reuse_stages),
            "stage_status": stage_status,
            "stage_timings_ms": {
                spec.stage_id: round(float(timings_ms.get(spec.stage_id, 0.0)), 6)
                for spec in plan.specs
            },
            "stage_compute_times_ms": {
                spec.stage_id: round(float(compute_times_ms.get(spec.stage_id, 0.0)), 6)
                for spec in plan.specs
            },
            "total_elapsed_ms": round(elapsed_ms, 6),
            "completed_stages": list(completed_stages),
            "snapshot_published": snapshot_published,
            "snapshot_path": str(snapshot_path) if snapshot_path is not None else None,
            "global_content_fingerprint": (
                final_info.manifest.get("global_content_fingerprint")
                if final_info is not None
                else None
            ),
            "final_pixel_diff": pixel_diff,
            "final_image": str(config.output_image) if final_pixels is not None else None,
            "final_image_pixel_fingerprint": final_image_sha,
            "status": status,
            "business_code": business_code,
        }
        if error is not None:
            entry["error"] = error
        return entry

    def _write_diagnostics(
        self,
        *,
        config: LoadedConfig,
        plan: PlanResult,
        entry: dict[str, Any],
        report_status: str,
        business_code: str | None,
        errors: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        report = build_report(
            status=report_status,
            business_code=business_code,
            edits=[entry],
            errors=errors,
            snapshot_version=config.snapshot_version,
            config_hash=config.config_hash,
            pipeline=[spec.stage_id for spec in plan.specs],
            dependencies=plan.dependencies,
            snapshot_path=entry.get("snapshot_path"),
            final_image=entry.get("final_image"),
            global_content_fingerprint=entry.get("global_content_fingerprint"),
        )
        write_report(config.report_json, report)
        render_dependency_graph(
            plan.specs,
            {stage_id: str(value) for stage_id, value in entry["stage_status"].items()},
            {key: float(value) for key, value in entry["stage_timings_ms"].items()},
            config.graph_png,
            snapshot_version=config.snapshot_version,
            title_suffix=report_status,
        )
        return report

    def _write_model_outputs(
        self,
        config: LoadedConfig,
        stage_data: Mapping[str, np.ndarray],
    ) -> tuple[np.ndarray, str]:
        final_pixels = np.asarray(stage_data["decode"], dtype=np.uint8)
        _write_png_atomic(config.output_image, final_pixels)
        config.intermediates_dir.mkdir(parents=True, exist_ok=True)
        for stage_id, data in stage_data.items():
            _write_npy_atomic(config.intermediates_dir / f"{stage_id}.npy", data)
        final_file_sha = array_fingerprint(final_pixels)
        return final_pixels, final_file_sha

    def run(
        self,
        config_path: Path | str,
        *,
        write_diagnostics: bool = True,
    ) -> RunOutcome:
        config = load_config(config_path)
        plan = self._build_plan(config)
        previous_pixels = self._previous_pixels(plan.resolved)
        controller = SignalController()
        controller.install()
        run_started = time.perf_counter()
        timings_ms: dict[str, float] = {spec.stage_id: 0.0 for spec in plan.specs}
        compute_times_ms: dict[str, float] = {spec.stage_id: 0.0 for spec in plan.specs}
        statuses = dict(plan.statuses)
        completed_stages: list[str] = []
        stage_data: dict[str, np.ndarray] = {}
        staging_dir: Path | None = None
        manifest: dict[str, Any] | None = None
        final_info: SnapshotInfo | None = None
        final_pixels: np.ndarray | None = None
        snapshot_path: Path | None = None
        snapshot_published = False

        try:
            already_published: SnapshotInfo | None = None
            if plan.resolved.current is not None and plan.resolved.current.version == config.snapshot_version:
                already_published = plan.resolved.current
            elif plan.resolved.target_complete is not None:
                already_published = plan.resolved.target_complete

            if already_published is not None:
                if plan.resolved.current is not already_published:
                    snapshot_path = self.store.activate_snapshot(already_published.path)
                else:
                    snapshot_path = already_published.path
                final_info = already_published
                for spec in plan.specs:
                    artifact = already_published.artifacts[spec.stage_id]
                    stage_data[spec.stage_id] = np.asarray(artifact.data).copy()
                    statuses[spec.stage_id] = "reused"
                    completed_stages.append(spec.stage_id)
                snapshot_published = True
            else:
                staging_dir, staging_info = self.store.ensure_staging(
                    snapshot_version=config.snapshot_version,
                    config_hash=config.config_hash,
                    pipeline=[spec.stage_id for spec in plan.specs],
                    resolved=plan.resolved,
                )
                manifest = staging_info.manifest
                progress = dict(manifest.get("progress", {}))
                progress.update(
                    complete=False,
                    status="in_progress",
                    failure=None,
                    interrupted_by=None,
                    interrupted_after_stage=None,
                )
                progress.pop("failure", None)
                manifest["progress"] = progress
                self.store.write_manifest(staging_dir, manifest)

                for spec in plan.specs:
                    if controller.requested:
                        break

                    stage_started = time.perf_counter()
                    try:
                        if plan.statuses.get(spec.stage_id) == "reused":
                            candidate = plan.candidates[spec.stage_id]
                            existing_record = manifest.get("stages", {}).get(spec.stage_id)
                            existing_artifact = staging_info.artifacts.get(spec.stage_id)
                            candidate_in_staging = (
                                candidate.path.resolve().is_relative_to(staging_dir.resolve())
                            )
                            if (
                                candidate_in_staging
                                and existing_artifact is not None
                                and existing_record is not None
                                and candidate.content_fingerprint
                                == existing_artifact.content_fingerprint
                            ):
                                artifact = existing_artifact
                            else:
                                artifact = save_artifact(
                                    staging_dir / artifact_relative_path(
                                        spec.stage_id, config.snapshot_version
                                    ),
                                    stage=spec.stage_id,
                                    kind=spec.artifact_kind,
                                    artifact_version=config.snapshot_version,
                                    parameter_fingerprint=spec.parameter_fingerprint,
                                    dependency_fingerprints=self._record_dependencies(
                                        spec,
                                        {
                                            dependency: plan.candidates.get(
                                                dependency,
                                                staging_info.artifacts.get(dependency),
                                            ).content_fingerprint
                                            for dependency in spec.dependencies
                                        },
                                    ),
                                    data=np.asarray(candidate.data),
                                )
                            statuses[spec.stage_id] = "reused"
                            elapsed_stage_ms = (time.perf_counter() - stage_started) * 1000.0
                            timings_ms[spec.stage_id] = 0.0
                            record = {
                                "artifact": artifact_relative_path(
                                    spec.stage_id, config.snapshot_version
                                ),
                                "artifact_sha256": artifact.file_sha256,
                                "artifact_version": config.snapshot_version,
                                "kind": spec.artifact_kind,
                                "content_fingerprint": artifact.content_fingerprint,
                                "parameter_fingerprint": spec.parameter_fingerprint,
                                "dependency_fingerprints": dict(
                                    artifact.meta["dependency_fingerprints"]
                                ),
                                "state": "reused",
                                "elapsed_ms": 0.0,
                            }
                            stage_data[spec.stage_id] = np.asarray(artifact.data).copy()
                        else:
                            manifest.get("stages", {}).pop(spec.stage_id, None)
                            upstream = {
                                dependency: stage_data[dependency]
                                for dependency in spec.dependencies
                            }
                            controller.fail_hook(spec.stage_id)
                            controller.delay_hook(spec.stage_id)
                            compute_started = time.perf_counter()
                            data = compute_stage(spec, config, upstream)
                            compute_elapsed_ms = (time.perf_counter() - compute_started) * 1000.0
                            dependency_fingerprints: dict[str, str] = {}
                            for dependency in spec.dependencies:
                                if dependency in plan.candidates:
                                    dependency_fingerprints[dependency] = plan.candidates[
                                        dependency
                                    ].content_fingerprint
                                else:
                                    # Recompute the content fingerprint from the actual
                                    # upstream data and metadata in the current manifest.
                                    record = manifest["stages"][dependency]
                                    dependency_fingerprints[dependency] = str(
                                        record["content_fingerprint"]
                                    )
                            artifact = save_artifact(
                                staging_dir / artifact_relative_path(
                                    spec.stage_id, config.snapshot_version
                                ),
                                stage=spec.stage_id,
                                kind=spec.artifact_kind,
                                artifact_version=config.snapshot_version,
                                parameter_fingerprint=spec.parameter_fingerprint,
                                dependency_fingerprints=dependency_fingerprints,
                                data=data,
                            )
                            elapsed_stage_ms = (time.perf_counter() - stage_started) * 1000.0
                            timings_ms[spec.stage_id] = elapsed_stage_ms
                            compute_times_ms[spec.stage_id] = compute_elapsed_ms
                            record = {
                                "artifact": artifact_relative_path(
                                    spec.stage_id, config.snapshot_version
                                ),
                                "artifact_sha256": artifact.file_sha256,
                                "artifact_version": config.snapshot_version,
                                "kind": spec.artifact_kind,
                                "content_fingerprint": artifact.content_fingerprint,
                                "parameter_fingerprint": spec.parameter_fingerprint,
                                "dependency_fingerprints": dependency_fingerprints,
                                "state": "recomputed",
                                "elapsed_ms": round(elapsed_stage_ms, 6),
                            }
                            stage_data[spec.stage_id] = np.asarray(data).copy()
                            statuses[spec.stage_id] = "recomputed"

                        manifest.setdefault("stages", {})[spec.stage_id] = record
                        completed_stages.append(spec.stage_id)
                        self.store.write_manifest(staging_dir, manifest)
                        staging_info.artifacts[spec.stage_id] = artifact
                    except Exception as exc:
                        manifest.get("stages", {}).pop(spec.stage_id, None)
                        statuses[spec.stage_id] = "failed"
                        progress = dict(manifest.get("progress", {}))
                        progress.update(
                            complete=False,
                            status="failed",
                            failed_stage=spec.stage_id,
                            failure={
                                "stage": spec.stage_id,
                                "exception_type": type(exc).__name__,
                                "message": str(exc),
                            },
                        )
                        manifest["progress"] = progress
                        self.store.write_manifest(staging_dir, manifest)
                        raise WorkbenchError(
                            E_STAGE_FAILURE,
                            f"stage {spec.stage_id} failed: {type(exc).__name__}: {exc}",
                            stage=spec.stage_id,
                            exception_type=type(exc).__name__,
                            details={
                                "snapshot_version": config.snapshot_version,
                                "invalidated_stages": list(plan.recompute_stages),
                                "reused_stages": list(plan.reuse_stages),
                                "completed_stages": list(completed_stages),
                                "staging_path": str(staging_dir),
                            },
                        ) from exc

                    controller.apply_test_hook_after(spec.stage_id)
                    if controller.requested:
                        progress = dict(manifest.get("progress", {}))
                        progress.update(
                            complete=False,
                            status="interrupted",
                            interrupted_by=controller.signal_name,
                            interrupted_after_stage=spec.stage_id,
                        )
                        manifest["progress"] = progress
                        self.store.write_manifest(staging_dir, manifest)
                        break
                    controller.crash_hook(spec.stage_id)

                if controller.requested:
                    elapsed_ms = (time.perf_counter() - run_started) * 1000.0
                    entry = self._entry(
                        config=config,
                        plan=plan,
                        statuses=statuses,
                        timings_ms=timings_ms,
                        compute_times_ms=compute_times_ms,
                        elapsed_ms=elapsed_ms,
                        completed_stages=completed_stages,
                        final_info=None,
                        final_pixels=None,
                        previous_pixels=previous_pixels,
                        snapshot_path=staging_dir,
                        snapshot_published=False,
                        status="interrupted",
                    )
                    report = (
                        self._write_diagnostics(
                            config=config,
                            plan=plan,
                            entry=entry,
                            report_status="interrupted",
                            business_code=None,
                        )
                        if write_diagnostics
                        else build_report(
                            status="interrupted",
                            business_code=None,
                            edits=[entry],
                            snapshot_version=config.snapshot_version,
                            config_hash=config.config_hash,
                            pipeline=[spec.stage_id for spec in plan.specs],
                            dependencies=plan.dependencies,
                            snapshot_path=str(staging_dir),
                        )
                    )
                    return RunOutcome(
                        status="interrupted",
                        exit_code=0,
                        entry=entry,
                        report=report,
                        final_pixels=None,
                        snapshot_path=staging_dir,
                    )

                progress = dict(manifest.get("progress", {}))
                progress.update(complete=True, status="complete")
                manifest["progress"] = progress
                manifest["final_image"] = str(config.output_image)
                self.store.write_manifest(staging_dir, manifest)

                final_pixels, final_fingerprint = self._write_model_outputs(config, stage_data)
                manifest["final_image_pixel_fingerprint"] = final_fingerprint
                manifest["final_image_file_sha256"] = None
                try:
                    from .canonical import file_sha256

                    manifest["final_image_file_sha256"] = file_sha256(config.output_image)
                except OSError:
                    pass
                self.store.write_manifest(staging_dir, manifest)
                snapshot_path = self.store.publish(staging_dir, config.snapshot_version)
                snapshot_published = True
                final_info = self.store._load_snapshot_dir(
                    snapshot_path,
                    strict=True,
                    expected_version=config.snapshot_version,
                    expected_config_hash=config.config_hash,
                )

            if final_pixels is None:
                final_pixels, final_fingerprint = self._write_model_outputs(config, stage_data)
                if final_info is not None:
                    final_info.manifest["final_image_pixel_fingerprint"] = final_fingerprint

            elapsed_ms = (time.perf_counter() - run_started) * 1000.0
            entry = self._entry(
                config=config,
                plan=plan,
                statuses=statuses,
                timings_ms=timings_ms,
                compute_times_ms=compute_times_ms,
                elapsed_ms=elapsed_ms,
                completed_stages=completed_stages,
                final_info=final_info,
                final_pixels=final_pixels,
                previous_pixels=previous_pixels,
                snapshot_path=snapshot_path,
                snapshot_published=snapshot_published,
                status="ok",
            )
            report = (
                self._write_diagnostics(
                    config=config,
                    plan=plan,
                    entry=entry,
                    report_status="ok",
                    business_code=None,
                )
                if write_diagnostics
                else build_report(
                    status="ok",
                    business_code=None,
                    edits=[entry],
                    snapshot_version=config.snapshot_version,
                    config_hash=config.config_hash,
                    pipeline=[spec.stage_id for spec in plan.specs],
                    dependencies=plan.dependencies,
                    snapshot_path=entry.get("snapshot_path"),
                    final_image=entry.get("final_image"),
                    global_content_fingerprint=entry.get("global_content_fingerprint"),
                )
            )
            return RunOutcome(
                status="ok",
                exit_code=0,
                entry=entry,
                report=report,
                final_pixels=final_pixels,
                snapshot_path=snapshot_path,
            )
        finally:
            controller.restore()
