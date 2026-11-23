from __future__ import annotations

import json
import os
import time
import traceback
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from .cache import ContentAddressedCache
from .compute import (
    block_stats_stage,
    execute_pipeline,
    normalize_stage,
    output_array_from_payload,
    output_stage,
    plan_stage,
)
from .diagnostics import build_job_diagnostics
from .errors import BusinessError, error_record
from .input_store import InputStore
from .locking import NonBlockingCommitLock
from .models import (
    STAGES,
    InjectionSpec,
    JobSpec,
)
from .snapshots import SnapshotStore
from .util import (
    atomic_write_bytes,
    atomic_write_json,
    canonical_json_text,
    float_key,
    npy_bytes,
    read_json,
    sanitize_component,
    sha256_bytes,
    sha256_value,
    tail_lines,
    utc_now,
)
from .workspace import Workspace


ENGINE_VERSION = "transformer-op-workbench/1.0"


class Engine:
    def __init__(
        self,
        root: str | Path,
        *,
        config_path: str | Path | None = None,
        injections: Iterable[InjectionSpec | str] = (),
        degrade_policy: str | None = None,
        mode: str = "incremental",
        verify_full: bool = True,
        hold_lock_seconds: float = 0.0,
    ) -> None:
        self.workspace = Workspace(root)
        self.workspace.initialize()
        config = self.workspace.load_config(config_path)
        parsed_injections = [
            InjectionSpec.parse(item) if isinstance(item, str) else item for item in injections
        ]
        config = replace(
            config,
            injections=tuple(config.injections) + tuple(parsed_injections),
            degrade_policy=degrade_policy or config.degrade_policy,
        )
        config.validate()
        self.config = config
        if mode not in {"incremental", "full"}:
            raise BusinessError(
                "E_POLICY_INVALID", "mode must be incremental or full", field="mode"
            )
        self.mode = mode
        self.verify_full = verify_full
        self.hold_lock_seconds = max(0.0, float(hold_lock_seconds))
        self.paths = self.workspace.paths

    def run(self, jobs_file: str | Path) -> dict[str, Any]:
        lock = NonBlockingCommitLock(self.workspace)
        with lock:
            if self.hold_lock_seconds:
                time.sleep(self.hold_lock_seconds)
            return self._run_locked(Path(jobs_file))

    def _run_locked(self, jobs_file: Path) -> dict[str, Any]:
        self.paths.ensure()
        cache = ContentAddressedCache(self.workspace)
        snapshots = SnapshotStore(self.workspace)
        inputs = InputStore(self.workspace)
        previous_state = self._load_run_state()

        tolerance_snapshot = snapshots.write_next(
            "tolerance", "global", self.config.tolerance.to_public()
        )
        cache.observe_snapshot_version("tolerance", "global", int(tolerance_snapshot["version"]))

        incoming = self._load_jobs_file(jobs_file)
        old_registry = self._load_registry()
        current_jobs, revision_only, merge_reasons = self._merge_registry(
            old_registry, incoming
        )

        errors: list[dict[str, Any]] = []
        skip_reasons: list[dict[str, Any]] = []
        for job_id, reasons in merge_reasons.items():
            for reason in reasons:
                skip_reasons.append({"job_id": job_id, **reason})

        input_arrays: dict[str, np.ndarray] = {}
        input_hashes: dict[str, str] = {}
        input_failures: dict[str, BusinessError] = {}
        for job_id in sorted(current_jobs):
            job = current_jobs[job_id]
            try:
                array, input_hash = inputs.materialize(job)
                input_arrays[job_id] = array
                input_hashes[job_id] = input_hash
            except BusinessError as exc:
                exc.job_id = exc.job_id or job_id
                exc.stage = exc.stage or "validate_plan"
                input_failures[job_id] = exc
        inputs.save()

        previous_jobs = previous_state.get("jobs", {})
        if not isinstance(previous_jobs, dict):
            raise BusinessError(
                "E_CACHE_CORRUPT", "run state jobs map is malformed", field="run_state.jobs"
            )
        policy_changed = previous_state.get("policy_signature") not in (
            None,
            self.config.policy_signature(),
        )
        tolerance_changed = previous_state.get("tolerance_signature") not in (
            None,
            self.config.tolerance_signature(),
        )

        affected_jobs: set[str] = set()
        unaffected_jobs: set[str] = set()
        for job_id, job in current_jobs.items():
            previous = previous_jobs.get(job_id)
            if (
                self.mode == "full"
                or not isinstance(previous, dict)
                or previous.get("semantic_signature") != job.semantic_signature()
                or previous.get("input_hash") != input_hashes.get(job_id)
                or previous.get("status") != "ok"
                or not previous.get("output_sha256")
                or policy_changed
                or job_id in input_failures
            ):
                affected_jobs.add(job_id)
            else:
                unaffected_jobs.add(job_id)

        population = sorted(current_jobs)
        node_records: dict[str, dict[str, dict[str, Any]]] = {
            job_id: {} for job_id in population
        }
        job_reports: dict[str, dict[str, Any]] = {}
        degradation_chains: list[dict[str, Any]] = []
        injection_records: list[dict[str, Any]] = []
        recomputed_nodes: list[str] = []
        reused_nodes: list[str] = []
        affected_nodes: list[str] = [
            f"{job_id}/{stage}" for job_id in sorted(affected_jobs) for stage in STAGES
        ]
        completed_job_ids: list[str] = []
        failed_job_ids: list[str] = []
        skipped_job_ids: list[str] = []
        consistency_jobs: dict[str, Any] = {}
        stage_payloads_for_snapshot: dict[str, dict[str, dict[str, Any]]] = {}
        plan_snapshot_jobs: dict[str, Any] = {}
        output_hashes: dict[str, str | None] = {}

        for job_id in population:
            job = current_jobs[job_id]
            if job_id in input_failures:
                failure = input_failures[job_id]
                self._record_failure(
                    job,
                    failure.stage or "validate_plan",
                    failure,
                    node_records[job_id],
                    errors,
                    degradation_chains,
                    skip_reasons,
                    injection_records,
                    self.config.degrade_policy,
                    failed_record_already=False,
                )
                self._remove_stale_output(job_id)
                failed_job_ids.append(job_id)
                output_hashes[job_id] = None
                job_reports[job_id] = self._job_report_shell(
                    job, input_hashes.get(job_id), job_id in affected_jobs
                )
                job_reports[job_id]["nodes"] = node_records[job_id]
                job_reports[job_id]["status"] = "failed"
                continue

            array = input_arrays[job_id]
            input_hash = input_hashes[job_id]
            stage_values: dict[str, dict[str, Any]] = {}
            upstream_hash: str | None = None
            failed = False

            for stage in STAGES:
                material = self._stage_key_material(
                    job, input_hash, stage, upstream_hash
                )
                cache_key = sha256_value(material)
                matching_injections = self._matching_injections(job_id, stage)
                if matching_injections:
                    failed = True
                    code = matching_injections[0].code
                    for injection in matching_injections:
                        record = {
                            "type": "injection",
                            "source": "parameter",
                            "stage": stage,
                            "job_id": job_id,
                            "code": injection.code,
                            "message": f"injected business error at {stage}",
                        }
                        injection_records.append(record)
                        errors.append(record)
                    injected_error = BusinessError(
                        code,
                        f"injected business error at stage {stage}",
                        job_id=job_id,
                        field="injection",
                        stage=stage,
                        category="injected",
                    )
                    self._record_failure(
                        job,
                        stage,
                        injected_error,
                        node_records[job_id],
                        errors,
                        degradation_chains,
                        skip_reasons,
                        injection_records,
                        self.config.degrade_policy,
                        failed_record_already=False,
                        cache_key=cache_key,
                        key_material=material,
                    )
                    break

                try:
                    cached = None if self.mode == "full" else cache.get(
                        cache_key, expected_job_id=job_id, expected_stage=stage
                    )
                    if cached is not None:
                        payload = cached["payload"]
                        if payload.get("key_material") != material:
                            raise BusinessError(
                                "E_CACHE_CORRUPT",
                                "cached key material does not match the requested key",
                                job_id=job_id,
                                field="cache.key_material",
                                stage=stage,
                            )
                        value = payload["value"]
                        status = "hit"
                        content_hash = cached["content_hash"]
                        artifact_version = cached["artifact_version"]
                        reused_nodes.append(f"{job_id}/{stage}")
                    else:
                        value = self._compute_stage(job, array, stage, stage_values)
                        payload = {
                            "schema": 1,
                            "job_id": job_id,
                            "stage": stage,
                            "key_material": material,
                            "value": value,
                        }
                        cached = cache.put(
                            cache_key, payload, stage=stage, job_id=job_id
                        )
                        status = "recomputed"
                        content_hash = cached["content_hash"]
                        artifact_version = cached["artifact_version"]
                        recomputed_nodes.append(f"{job_id}/{stage}")
                    upstream_hash = content_hash
                    stage_values[stage] = value
                    node_records[job_id][stage] = {
                        "schema": 1,
                        "job_id": job_id,
                        "stage": stage,
                        "status": status,
                        "cache_key": cache_key,
                        "content_hash": content_hash,
                        "artifact_version": artifact_version,
                        "key_material": material,
                        "affected": job_id in affected_jobs,
                        "reason": (
                            "cache_key_match"
                            if status == "hit"
                            else (
                                "invalidated_suffix"
                                if job_id in affected_jobs
                                else "cache_miss_repair"
                            )
                        ),
                    }
                except BusinessError as exc:
                    failed = True
                    exc.job_id = exc.job_id or job_id
                    exc.stage = exc.stage or stage
                    self._record_failure(
                        job,
                        stage,
                        exc,
                        node_records[job_id],
                        errors,
                        degradation_chains,
                        skip_reasons,
                        injection_records,
                        self.config.degrade_policy,
                        failed_record_already=False,
                        cache_key=cache_key,
                        key_material=material,
                    )
                    break
                except Exception as exc:  # noqa: BLE001 - explicitly attributed below
                    failed = True
                    attributed = BusinessError(
                        "E_UNATTRIBUTED",
                        f"unattributed {type(exc).__name__} at {stage}: {exc}",
                        job_id=job_id,
                        field="internal",
                        stage=stage,
                        category="unattributed",
                        details={
                            "exception_type": type(exc).__name__,
                            "traceback": traceback.format_exc(limit=8),
                        },
                    )
                    self._record_failure(
                        job,
                        stage,
                        attributed,
                        node_records[job_id],
                        errors,
                        degradation_chains,
                        skip_reasons,
                        injection_records,
                        self.config.degrade_policy,
                        failed_record_already=False,
                        cache_key=cache_key,
                        key_material=material,
                    )
                    break

            if failed:
                self._remove_stale_output(job_id)
                job_reports[job_id] = self._job_report_shell(
                    job, input_hash, job_id in affected_jobs
                )
                job_reports[job_id]["nodes"] = node_records[job_id]
                job_reports[job_id]["status"] = (
                    "skipped" if self.config.degrade_policy == "skip" else "failed"
                )
                if job_reports[job_id]["status"] == "skipped":
                    skipped_job_ids.append(job_id)
                else:
                    failed_job_ids.append(job_id)
                output_hashes[job_id] = None
                continue

            stage_payloads_for_snapshot[job_id] = stage_values
            plan_snapshot_jobs[job_id] = stage_values["validate_plan"]
            output_payload = stage_values["output"]
            output_array = output_array_from_payload(output_payload)
            output_path = self.paths.outputs / f"{sanitize_component(job_id)}.npy"
            atomic_write_bytes(output_path, npy_bytes(output_array))
            output_hashes[job_id] = output_payload["output_npy_sha256"]

            diagnostics, issues = build_job_diagnostics(
                job,
                stage_values,
                self.config.tolerance,
                self.paths.diagnostics / "deviation" / f"{sanitize_component(job_id)}.npz",
                root=self.paths.root,
            )
            for issue in issues:
                errors.append(issue)
            if issues:
                failed_job_ids.append(job_id)
                job_status = "failed"
            else:
                job_status = "ok"

            if self.verify_full:
                independent = self._independent_full_recompute(
                    job, array, output_payload["output_npy_sha256"]
                )
            else:
                independent = {
                    "byte_identical": None,
                    "skipped": True,
                    "incremental_sha256": output_payload["output_npy_sha256"],
                    "full_recompute_sha256": None,
                    "reason": "--no-verify / verify_full=False",
                }
            if self.verify_full and not independent["byte_identical"]:
                inconsistency = {
                    "type": "error",
                    "source": "verification",
                    "code": "E_INCONSISTENT",
                    "message": "incremental output is not byte-identical to independent full recompute",
                    "stage": "output",
                    "job_id": job_id,
                    "field": "output_bytes",
                    "details": independent,
                }
                errors.append(inconsistency)
                if job_status == "ok":
                    failed_job_ids.append(job_id)
                    job_status = "failed"
            if job_status == "ok":
                completed_job_ids.append(job_id)
            consistency_jobs[job_id] = independent

            job_reports[job_id] = self._job_report_shell(
                job, input_hash, job_id in affected_jobs
            )
            job_reports[job_id].update(
                {
                    "nodes": node_records[job_id],
                    "diagnostics": diagnostics,
                    "consistency": independent,
                    "output_sha256": output_payload["output_npy_sha256"],
                    "output_path": str(output_path),
                    "status": job_status,
                }
            )
            skip_reasons.append(
                {
                    "job_id": job_id,
                    "stage": "all",
                    "reason": "completed",
                    "message": "all four stages produced or reused content-addressed artifacts",
                }
            )

        snapshot_records = [
            {
                "kind": tolerance_snapshot["kind"],
                "key": tolerance_snapshot["key"],
                "version": tolerance_snapshot["version"],
                "content_checksum": tolerance_snapshot["content_checksum"],
                "replaced_whole": True,
                "changed": tolerance_snapshot.get("changed"),
            }
        ]
        if plan_snapshot_jobs:
            plan_snapshot = snapshots.write_next(
                "plan",
                "global",
                {
                    "engine": ENGINE_VERSION,
                    "policy_signature": self.config.policy_signature(),
                    "jobs": plan_snapshot_jobs,
                },
            )
            cache.observe_snapshot_version(
                "plan", "global", int(plan_snapshot["version"])
            )
            snapshot_records.append(
                {
                    "kind": plan_snapshot["kind"],
                    "key": plan_snapshot["key"],
                    "version": plan_snapshot["version"],
                    "content_checksum": plan_snapshot["content_checksum"],
                    "replaced_whole": True,
                    "changed": plan_snapshot.get("changed"),
                }
            )
        for job_id, stage_values in sorted(stage_payloads_for_snapshot.items()):
            stats_snapshot = snapshots.write_next(
                "stats",
                job_id,
                {
                    "job_id": job_id,
                    "semantic_signature": current_jobs[job_id].semantic_signature(),
                    "input_hash": input_hashes.get(job_id),
                    "block_stats_hash": node_records[job_id]["block_stats"].get(
                        "content_hash"
                    ),
                    "normalize_hash": node_records[job_id]["normalize"].get(
                        "content_hash"
                    ),
                    "merged_stats": stage_values["normalize"]["merged_stats"],
                    "whole_stats": stage_values["normalize"]["whole_stats"],
                    "normalizer": stage_values["normalize"]["normalizer"],
                },
            )
            cache.observe_snapshot_version(
                "stats", job_id, int(stats_snapshot["version"])
            )
            snapshot_records.append(
                {
                    "kind": stats_snapshot["kind"],
                    "key": stats_snapshot["key"],
                    "version": stats_snapshot["version"],
                    "content_checksum": stats_snapshot["content_checksum"],
                    "replaced_whole": True,
                    "changed": stats_snapshot.get("changed"),
                }
            )

        cache.save()
        snapshot_list = snapshots.list()
        current_registry = {
            job_id: job.to_public() for job_id, job in sorted(current_jobs.items())
        }
        self.workspace.save_jobs_registry(current_registry)
        atomic_write_json(
            self.paths.nodes,
            {
                "schema": 1,
                "generated_at": utc_now(),
                "nodes": node_records,
            },
            pretty=True,
        )
        new_run_state = {
            "schema": 1,
            "policy_signature": self.config.policy_signature(),
            "tolerance_signature": self.config.tolerance_signature(),
            "degrade_policy": self.config.degrade_policy,
            "jobs": {
                job_id: {
                    "semantic_signature": current_jobs[job_id].semantic_signature(),
                    "input_hash": input_hashes.get(job_id),
                    "output_sha256": output_hashes.get(job_id),
                    "revision": current_jobs[job_id].revision,
                    "status": job_reports[job_id].get("status", "failed"),
                }
                for job_id in sorted(current_jobs)
            },
        }
        atomic_write_json(self.paths.run_state, new_run_state, pretty=True)

        status = "ok"
        if failed_job_ids:
            status = "failed"
        elif skipped_job_ids or errors:
            status = "degraded"
        recomputed_jobs = sorted(
            {item.split("/", 1)[0] for item in recomputed_nodes}
        )
        reused_jobs = sorted({item.split("/", 1)[0] for item in reused_nodes})
        if not consistency_jobs:
            all_byte_identical: bool | None = False
        elif not self.verify_full:
            all_byte_identical = None
        else:
            all_byte_identical = all(
                bool(item["byte_identical"]) for item in consistency_jobs.values()
            )
        report: dict[str, Any] = {
            "schema": 1,
            "run_id": uuid.uuid4().hex,
            "generated_at": utc_now(),
            "engine": ENGINE_VERSION,
            "workspace": str(self.paths.root),
            "mode": self.mode,
            "status": status,
            "jobs_file": str(Path(jobs_file).resolve()),
            "config": self.config.to_public(),
            "jobs": job_reports,
            "completed_job_ids": sorted(completed_job_ids),
            "failed_job_ids": sorted(set(failed_job_ids)),
            "skipped_job_ids": sorted(set(skipped_job_ids)),
            "invalidation": {
                "affected_jobs": sorted(affected_jobs),
                "unaffected_jobs": sorted(unaffected_jobs),
                "recomputed_jobs": recomputed_jobs,
                "reused_jobs": reused_jobs,
                "affected_nodes": affected_nodes,
                "recomputed_nodes": sorted(recomputed_nodes),
                "reused_nodes": sorted(reused_nodes),
                "policy_changed": bool(policy_changed),
                "tolerance_changed": bool(tolerance_changed),
                "skip_reasons": skip_reasons,
            },
            "degradation_chains": degradation_chains,
            "injection_records": injection_records,
            "errors": errors,
            "consistency": {
                "all_byte_identical": all_byte_identical,
                "jobs": consistency_jobs,
            },
            "snapshots": snapshot_records,
            "snapshot_inventory": snapshot_list,
        }
        self._append_error_history(errors, report["run_id"])
        report["recent_error_codes"] = self._recent_error_codes(limit=25)
        report["graph"] = self._build_graph(job_reports)
        atomic_write_json(self.paths.latest_report, report, pretty=True)
        try:
            from .report import render_workbench

            render_workbench(report, self.paths.html_report)
            report["workbench_html"] = str(self.paths.html_report)
            report["report_path"] = str(self.paths.latest_report)
            atomic_write_json(self.paths.latest_report, report, pretty=True)
        except Exception:
            record = {
                "type": "error",
                "source": "workbench",
                "code": "E_UNATTRIBUTED",
                "message": "workbench rendering failed but core artifacts were committed",
                "stage": "report",
                "job_id": None,
                "field": "html",
                "details": {"traceback": traceback.format_exc(limit=8)},
            }
            errors.append(record)
            report["errors"] = errors
            if report.get("status") == "ok":
                report["status"] = "degraded"
            report["recent_error_codes"] = self._recent_error_codes(limit=25)
            atomic_write_json(self.paths.latest_report, report, pretty=True)
        return report

    def _load_jobs_file(self, path: Path) -> list[JobSpec]:
        if not path.exists():
            raise BusinessError(
                "E_INPUT_INVALID", f"jobs file does not exist: {path}", field=str(path)
            )
        jobs: list[JobSpec] = []
        seen: dict[str, JobSpec] = {}
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as exc:
            raise BusinessError(
                "E_INPUT_INVALID", f"cannot read jobs file: {exc}", field=str(path)
            ) from exc
        for line_no, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                raise BusinessError(
                    "E_INPUT_INVALID",
                    f"invalid JSONL at line {line_no}: {exc}",
                    field=f"line:{line_no}",
                ) from exc
            job = JobSpec.from_dict(raw, source=f"{path}:{line_no}")
            existing = seen.get(job.job_id)
            if existing is not None:
                if job.revision < existing.revision:
                    raise BusinessError(
                        "E_REVISION_STALE",
                        "a later line contains an older revision for the same job_id",
                        job_id=job.job_id,
                        field="revision",
                    )
                if (
                    job.revision == existing.revision
                    and job.semantic_signature() != existing.semantic_signature()
                ):
                    raise BusinessError(
                        "E_REVISION_STALE",
                        "same job revision has conflicting semantic configuration",
                        job_id=job.job_id,
                        field="revision",
                    )
            seen[job.job_id] = job
            jobs.append(job)
        if not jobs:
            raise BusinessError("E_INPUT_INVALID", "jobs file contains no job records", field="jobs")
        return list(seen.values())

    def _load_registry(self) -> dict[str, JobSpec]:
        raw = self.workspace.load_jobs_registry()
        result: dict[str, JobSpec] = {}
        for job_id, public in raw.items():
            try:
                result[job_id] = JobSpec.from_dict(public)
            except BusinessError as exc:
                raise BusinessError(
                    "E_CACHE_CORRUPT",
                    f"jobs registry entry {job_id} is invalid: {exc.message}",
                    job_id=job_id,
                    field=exc.field,
                ) from exc
        return result

    def _merge_registry(
        self, old: dict[str, JobSpec], incoming: list[JobSpec]
    ) -> tuple[dict[str, JobSpec], set[str], dict[str, list[dict[str, Any]]]]:
        merged = dict(old)
        revision_only: set[str] = set()
        reasons: dict[str, list[dict[str, Any]]] = {}
        for job in incoming:
            existing = merged.get(job.job_id)
            if existing is None:
                merged[job.job_id] = job
                continue
            if job.revision < existing.revision:
                raise BusinessError(
                    "E_REVISION_STALE",
                    f"revision {job.revision} is older than registered revision {existing.revision}",
                    job_id=job.job_id,
                    field="revision",
                    details={"existing": existing.revision, "incoming": job.revision},
                )
            if job.revision == existing.revision:
                if job.semantic_signature() != existing.semantic_signature():
                    raise BusinessError(
                        "E_REVISION_STALE",
                        "same revision has conflicting semantic configuration",
                        job_id=job.job_id,
                        field="revision",
                    )
                if job.batch_no != existing.batch_no:
                    reasons.setdefault(job.job_id, []).append(
                        {
                            "stage": "all",
                            "reason": "batch_metadata_only",
                            "message": "batch_no changed without a revision or semantic change",
                        }
                    )
                    revision_only.add(job.job_id)
                merged[job.job_id] = job
                continue
            if job.semantic_signature() == existing.semantic_signature():
                revision_only.add(job.job_id)
                reasons.setdefault(job.job_id, []).append(
                    {
                        "stage": "all",
                        "reason": "revision_metadata_only",
                        "message": "revision increased but semantic configuration and seed are unchanged",
                    }
                )
            merged[job.job_id] = job
        return merged, revision_only, reasons

    def _stage_key_material(
        self,
        job: JobSpec,
        input_hash: str,
        stage: str,
        upstream_hash: str | None,
    ) -> dict[str, Any]:
        return {
            "schema": 1,
            "engine": ENGINE_VERSION,
            "stage": stage,
            "job_id": job.job_id,
            "input_hash": input_hash,
            "op_kind": job.op_kind,
            "input_shape": list(job.input_shape),
            "block_size": job.block_size,
            "axis": job.axis_normalized,
            "eps": float_key(job.eps),
            "dtype": job.dtype,
            "policy_signature": self.config.policy_signature(),
            "upstream_hash": upstream_hash,
        }

    def _compute_stage(
        self,
        job: JobSpec,
        array: np.ndarray,
        stage: str,
        stage_values: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        if stage == "validate_plan":
            return plan_stage(job, array, self.config.policy)
        if stage == "block_stats":
            return block_stats_stage(
                job, array, stage_values["validate_plan"], self.config.policy
            )
        if stage == "normalize":
            plan = stage_values["validate_plan"]
            eps_effective = float(plan["guards"]["eps"]["effective_eps"])
            return normalize_stage(
                job,
                array,
                plan,
                stage_values["block_stats"],
                self.config.policy,
                eps_effective,
            )
        if stage == "output":
            return output_stage(
                job,
                array,
                stage_values["validate_plan"],
                stage_values["normalize"],
                self.config.policy,
            )
        raise BusinessError(
            "E_UNATTRIBUTED",
            f"unknown stage {stage}",
            job_id=job.job_id,
            stage=stage,
            field="stage",
        )

    def _matching_injections(self, job_id: str, stage: str) -> list[InjectionSpec]:
        return [
            injection
            for injection in self.config.injections
            if injection.stage == stage
            and (injection.job_id is None or injection.job_id == job_id)
        ]

    def _record_failure(
        self,
        job: JobSpec,
        failed_stage: str,
        error: BusinessError,
        nodes: dict[str, dict[str, Any]],
        errors: list[dict[str, Any]],
        degradation_chains: list[dict[str, Any]],
        skip_reasons: list[dict[str, Any]],
        injection_records: list[dict[str, Any]],
        degrade_policy: str,
        *,
        failed_record_already: bool,
        cache_key: str | None = None,
        key_material: dict[str, Any] | None = None,
    ) -> None:
        if error.code == "E_STAGE_BUSY":
            category = "busy"
        elif error.category == "injected":
            category = "injected"
        elif error.category == "unattributed":
            category = "unattributed"
        else:
            category = "validation" if failed_stage == "validate_plan" else "stage_failure"
        if not failed_record_already:
            record = error_record(error, source="stage", injected=error.category == "injected")
            if error.code == "E_UNATTRIBUTED":
                record["source"] = "unattributed"
            if not any(
                item.get("code") == error.code
                and item.get("stage") == failed_stage
                and item.get("job_id") == job.job_id
                and item.get("type") == record.get("type")
                for item in errors
            ):
                errors.append(record)
        nodes[failed_stage] = {
            "schema": 1,
            "job_id": job.job_id,
            "stage": failed_stage,
            "status": "failed",
            "cache_key": cache_key,
            "key_material": key_material,
            "error_code": error.code,
            "category": category,
            "affected": True,
        }
        downstream = STAGES[STAGES.index(failed_stage) + 1 :]
        degradation_category = "skip" if degrade_policy == "skip" else "hard_fail"
        for stage in downstream:
            chain = {
                "type": "degradation",
                "job_id": job.job_id,
                "stage": stage,
                "upstream_stage": failed_stage,
                "upstream_code": error.code,
                "category": degradation_category,
                "policy": degrade_policy,
                "message": f"{degrade_policy} policy blocks {stage} after {failed_stage}",
            }
            degradation_chains.append(chain)
            nodes[stage] = {
                "schema": 1,
                "job_id": job.job_id,
                "stage": stage,
                "status": "skipped",
                "reason": degradation_category,
                "upstream_stage": failed_stage,
                "upstream_code": error.code,
                "category": degradation_category,
                "affected": True,
            }
            skip_reasons.append(
                {
                    "job_id": job.job_id,
                    "stage": stage,
                    "reason": degradation_category,
                    "upstream_stage": failed_stage,
                    "upstream_code": error.code,
                    "message": chain["message"],
                }
            )

    def _job_report_shell(
        self, job: JobSpec, input_hash: str | None, affected: bool
    ) -> dict[str, Any]:
        return {
            "job_id": job.job_id,
            "spec": job.to_public(),
            "input_hash": input_hash,
            "affected": affected,
            "nodes": {},
            "status": "pending",
        }

    def _independent_full_recompute(
        self, job: JobSpec, array: np.ndarray, incremental_sha: str
    ) -> dict[str, Any]:
        try:
            pipeline = execute_pipeline(job, array, self.config.policy)
            output_array = output_array_from_payload(pipeline["output"])
            full_sha = sha256_bytes(npy_bytes(output_array))
            return {
                "byte_identical": full_sha == incremental_sha,
                "incremental_sha256": incremental_sha,
                "full_recompute_sha256": full_sha,
                "full_recompute_policy": "fresh no-cache four-stage pipeline",
                "stage_hashes": {
                    stage: sha256_value(payload) for stage, payload in pipeline.items()
                },
            }
        except BusinessError as exc:
            return {
                "byte_identical": False,
                "incremental_sha256": incremental_sha,
                "full_recompute_sha256": None,
                "error": exc.to_dict(),
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "byte_identical": False,
                "incremental_sha256": incremental_sha,
                "full_recompute_sha256": None,
                "error": {
                    "code": "E_UNATTRIBUTED",
                    "message": f"independent verification failed: {type(exc).__name__}: {exc}",
                    "traceback": traceback.format_exc(limit=8),
                },
            }

    def _remove_stale_output(self, job_id: str) -> None:
        path = self.paths.outputs / f"{sanitize_component(job_id)}.npy"
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    def _load_run_state(self) -> dict[str, Any]:
        raw = read_json(
            self.paths.run_state,
            default={
                "schema": 1,
                "policy_signature": None,
                "tolerance_signature": None,
                "jobs": {},
            },
        )
        if not isinstance(raw, dict):
            raise BusinessError(
                "E_CACHE_CORRUPT", "run state is malformed", field=str(self.paths.run_state)
            )
        return raw

    def _append_error_history(self, errors: list[dict[str, Any]], run_id: str) -> None:
        if not errors:
            return
        self.paths.errors.parent.mkdir(parents=True, exist_ok=True)
        stamp = utc_now()
        try:
            with self.paths.errors.open("a", encoding="utf-8") as handle:
                for error in errors:
                    record = {
                        "at": stamp,
                        "run_id": run_id,
                        "code": error.get("code"),
                        "stage": error.get("stage"),
                        "job_id": error.get("job_id"),
                        "field": error.get("field"),
                        "message": error.get("message"),
                    }
                    handle.write(canonical_json_text(record) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError:
            pass

    def _recent_error_codes(self, *, limit: int = 25) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for line in tail_lines(self.paths.errors, limit):
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                records.append(item)
        return list(reversed(records))

    @staticmethod
    def _build_graph(job_reports: dict[str, dict[str, Any]]) -> dict[str, Any]:
        nodes: list[dict[str, Any]] = []
        edges: list[dict[str, str]] = []
        for job_id, report in sorted(job_reports.items()):
            previous: str | None = None
            for stage in STAGES:
                node_id = f"{job_id}/{stage}"
                node = report.get("nodes", {}).get(stage, {})
                nodes.append(
                    {
                        "id": node_id,
                        "job_id": job_id,
                        "stage": stage,
                        "status": node.get("status", "missing"),
                        "content_hash": node.get("content_hash"),
                        "cache_key": node.get("cache_key"),
                        "error_code": node.get("error_code"),
                    }
                )
                if previous is not None:
                    edges.append({"from": previous, "to": node_id})
                previous = node_id
        return {"nodes": nodes, "edges": edges}


def record_external_error(root: str | Path, error: BusinessError) -> None:
    try:
        workspace = Workspace(root)
        workspace.initialize()
        path = workspace.paths.errors
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "at": utc_now(),
            "run_id": None,
            "code": error.code,
            "stage": error.stage,
            "job_id": error.job_id,
            "field": error.field,
            "message": error.message,
        }
        with path.open("a", encoding="utf-8") as handle:
            handle.write(canonical_json_text(record) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        return

