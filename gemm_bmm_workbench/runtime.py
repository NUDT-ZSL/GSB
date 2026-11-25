from __future__ import annotations

import copy
import os
import shutil
import signal
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import numpy as np

from .cache import ContentCache, StagedCacheCommit, cache_key_report, make_node_key
from .canonical import (
    BASE_LOGICAL_MS,
    append_jsonl,
    atomic_write_json,
    read_json,
    sha256_json,
    write_npy_atomic,
)
from .errors import Codes, WorkbenchError, error_record
from .kernels import tiled_multiply_accumulate, verify_against_reference
from .ledger import LedgerManager
from .planner import (
    aggregate_key_parts,
    build_plan,
    derive_node_keys,
    accumulation_key_parts,
    make_node_key as unused_make_node_key,
    plan_key_parts,
)
from .resources import Budget, CommitLock, choose_effective_block_size, estimate_accum_buffer_bytes
from .sampling import deterministic_arrays, load_npz_arrays
from .snapshots import STAGES, SnapshotManager
from .specs import (
    check_duplicate_job_ids,
    check_revision_progress,
    normalize_job,
    spec_fingerprint,
    validate_arrays,
)


class Workbench:
    def __init__(self, state_dir: str | Path, budget: Budget | dict[str, Any] | None = None) -> None:
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        if budget is None:
            self.budget = Budget()
        elif isinstance(budget, dict):
            self.budget = Budget(**budget).validated()
        else:
            self.budget = budget.validated()
        self.cache = ContentCache(self.state_dir / "cache")
        self.ledger = LedgerManager(self.state_dir)
        self.snapshots = SnapshotManager(self.state_dir)
        self.pending_path = self.state_dir / "pending-run.json"
        self.committed_state_path = self.state_dir / "committed-state.json"
        self.incremental_report_path = self.state_dir / "incremental-report.json"
        self.cache_report_path = self.state_dir / "cache-report.json"
        self.code_log_path = self.state_dir / "logs" / "business-codes.jsonl"
        self.result_dir = self.state_dir / "results"
        self.diagnosis_dir = self.state_dir / "diagnostics"
        for path in (self.result_dir, self.diagnosis_dir, self.code_log_path.parent):
            path.mkdir(parents=True, exist_ok=True)
        self._fault_chunk_count = 0

    def record_error(self, exc: BaseException, *, source: str = "cli") -> dict[str, Any]:
        record = error_record(exc, source=source)
        existing = []
        if self.code_log_path.exists():
            existing = self.code_log_path.read_text(encoding="utf-8").splitlines()
        record["seq"] = len([line for line in existing if line.strip()]) + 1
        record["logical_ms"] = BASE_LOGICAL_MS + record["seq"] * 1000
        append_jsonl(self.code_log_path, [record])
        return record

    def recent_codes(self, limit: int = 20) -> list[dict[str, Any]]:
        if not self.code_log_path.exists():
            return []
        lines = self.code_log_path.read_text(encoding="utf-8").splitlines()
        records = [read_json_line(line) for line in lines[-limit:] if line.strip()]
        return list(reversed(records))

    def load_committed_state(self) -> dict[str, Any]:
        if not self.committed_state_path.exists():
            return {"version": 1, "jobs": {}}
        return read_json(self.committed_state_path)

    def _resolve_arrays(
        self,
        spec: dict[str, Any],
        arrays_by_job: dict[str, dict[str, np.ndarray]] | None,
        npz_path: str | Path | None,
    ) -> dict[str, np.ndarray]:
        if arrays_by_job is not None and spec["job_id"] in arrays_by_job:
            return arrays_by_job[spec["job_id"]]
        if npz_path is not None:
            return load_npz_arrays(npz_path, spec)
        return deterministic_arrays(spec)

    def prepare_specs(
        self,
        specs: list[dict[str, Any]],
        *,
        parallel_jobs: int = 1,
        arrays_by_job: dict[str, dict[str, np.ndarray]] | None = None,
        npz_path: str | Path | None = None,
    ) -> tuple[list[dict[str, Any]], int, list[dict[str, Any]]]:
        if not isinstance(parallel_jobs, int) or parallel_jobs <= 0:
            raise WorkbenchError(Codes.SCHEMA_INVALID, "parallel_jobs must be a positive integer")
        global_degradations: list[dict[str, Any]] = []
        effective_parallel = parallel_jobs
        if parallel_jobs > self.budget.max_parallel_jobs:
            if self.budget.resource_policy == "error":
                raise WorkbenchError(
                    Codes.RESOURCE_LIMIT,
                    "requested parallel recompute jobs exceed max_parallel_jobs",
                    {
                        "requested": parallel_jobs,
                        "limit": self.budget.max_parallel_jobs,
                        "category": "PARALLEL_JOB_LIMIT",
                    },
                )
            effective_parallel = self.budget.max_parallel_jobs
            global_degradations.append(
                {
                    "category": "PARALLELISM_CLAMPED",
                    "requested_parallel_jobs": parallel_jobs,
                    "effective_parallel_jobs": effective_parallel,
                    "limit": self.budget.max_parallel_jobs,
                }
            )

        prepared: list[dict[str, Any]] = []
        for spec in sorted(specs, key=lambda item: item["job_id"]):
            arrays = self._resolve_arrays(spec, arrays_by_job, npz_path)
            array_hashes = validate_arrays(spec, arrays)
            effective_block, degradations = choose_effective_block_size(
                op_kind=spec["op_kind"],
                matrix_shapes=spec["matrix_shapes"],
                requested_block_size=spec["block_size"],
                budget=self.budget,
                parallel_jobs=effective_parallel,
            )
            memory_estimate = estimate_accum_buffer_bytes(
                op_kind=spec["op_kind"],
                matrix_shapes=spec["matrix_shapes"],
                block_size=effective_block,
                parallel_jobs=effective_parallel,
            )
            input_hash = sha256_json(
                {
                    "spec_hash": spec_fingerprint(spec),
                    "array_hashes": array_hashes,
                }
            )
            prepared.append(
                {
                    "spec": spec,
                    "arrays": arrays,
                    "array_hashes": array_hashes,
                    "spec_hash": spec_fingerprint(spec),
                    "input_hash": input_hash,
                    "effective_block_size": effective_block,
                    "degradations": degradations,
                    "memory_estimate": memory_estimate,
                }
            )
        return prepared, effective_parallel, global_degradations

    def submit_records(
        self,
        records: list[dict[str, Any]],
        *,
        parallel_jobs: int = 1,
        arrays_by_job: dict[str, dict[str, np.ndarray]] | None = None,
        npz_path: str | Path | None = None,
        run_now: bool = True,
        kill_after_chunks: int | None = None,
    ) -> dict[str, Any]:
        check_duplicate_job_ids(records)
        normalized_incoming = [normalize_job(record) for record in records]
        if len(normalized_incoming) > self.budget.max_jobs_per_stage:
            raise WorkbenchError(
                Codes.RESOURCE_LIMIT,
                "single stage job count exceeds max_jobs_per_stage",
                {"submitted_jobs": len(normalized_incoming), "limit": self.budget.max_jobs_per_stage},
            )

        committed_state = self.load_committed_state()
        previous_jobs = committed_state.get("jobs", {})
        check_revision_progress(normalized_incoming, previous_jobs)

        merged_specs: dict[str, dict[str, Any]] = {
            job_id: copy.deepcopy(job["spec"]) for job_id, job in previous_jobs.items()
        }
        for spec in normalized_incoming:
            merged_specs[spec["job_id"]] = spec

        if len(merged_specs) > self.budget.max_jobs_per_stage:
            raise WorkbenchError(
                Codes.RESOURCE_LIMIT,
                "active single-stage job count exceeds max_jobs_per_stage after incremental merge",
                {"active_jobs": len(merged_specs), "limit": self.budget.max_jobs_per_stage},
            )

        prepared, effective_parallel, global_degradations = self.prepare_specs(
            list(merged_specs.values()),
            parallel_jobs=parallel_jobs,
            arrays_by_job=arrays_by_job,
            npz_path=npz_path,
        )
        impact = self.build_impact_report(prepared, committed_state, effective_parallel, global_degradations)
        run_id = "run-" + sha256_json(
            {
                "specs": [item["spec"] for item in prepared],
                "budget": self.budget.to_dict(),
                "parallel_jobs": effective_parallel,
            }
        )[:16]
        pending = {
            "run_id": run_id,
            "specs": [item["spec"] for item in prepared],
            "parallel_jobs": effective_parallel,
            "budget": self.budget.to_dict(),
            "impact": impact,
        }
        with CommitLock(self.state_dir / "locks" / "commit.lock"):
            atomic_write_json(self.pending_path, pending)
            atomic_write_json(self.incremental_report_path, impact)
        if not run_now:
            return impact
        return self.run_pending(kill_after_chunks=kill_after_chunks)

    def build_impact_report(
        self,
        prepared: list[dict[str, Any]],
        committed_state: dict[str, Any],
        effective_parallel: int,
        global_degradations: list[dict[str, Any]],
    ) -> dict[str, Any]:
        previous_jobs = committed_state.get("jobs", {})
        recomputed_nodes: list[str] = []
        reused_nodes: list[str] = []
        affected_jobs: list[str] = []
        recomputed_jobs: list[str] = []
        fully_reused_jobs: list[str] = []
        job_reports: list[dict[str, Any]] = []

        for item in prepared:
            spec = item["spec"]
            job_id = spec["job_id"]
            old = previous_jobs.get(job_id)
            plan = build_plan(
                spec,
                item["array_hashes"],
                effective_block_size=item["effective_block_size"],
                degradations=item["degradations"],
                memory_estimate=item["memory_estimate"],
            )
            plan_parts = plan_key_parts(spec, item["array_hashes"], plan)
            plan_key = make_node_key("validation_plan", plan_parts)
            accum_parts = accumulation_key_parts(spec, item["array_hashes"], plan)
            accum_key = make_node_key("block_accumulate", accum_parts)

            changed_fields: list[str] = []
            if old is None:
                changed_fields.append("new_job")
            else:
                old_spec = old.get("spec", {})
                for field in (
                    "op_kind",
                    "matrix_shapes",
                    "dtype",
                    "layout",
                    "block_size",
                    "alpha",
                    "beta",
                    "revision",
                    "tolerance",
                ):
                    if old_spec.get(field) != spec.get(field):
                        changed_fields.append(field)
                if old.get("array_hashes") != item["array_hashes"]:
                    changed_fields.append("matrix_arrays")

            old_nodes = (old or {}).get("nodes", {})
            old_plan_key = old_nodes.get("validation_plan", {}).get("key")
            old_accum_key = old_nodes.get("block_accumulate", {}).get("key")
            plan_recompute = old is None or plan_key != old_plan_key
            impact_plan = dict(plan)
            if not plan_recompute:
                impact_plan["plan_hash"] = old_nodes.get("validation_plan", {}).get(
                    "payload_hash", plan["plan_hash"]
                )
            accum_parts = accumulation_key_parts(spec, item["array_hashes"], impact_plan)
            accum_key = make_node_key("block_accumulate", accum_parts)
            accum_recompute = plan_recompute or accum_key != old_accum_key
            aggregate_recompute = accum_recompute

            statuses = {
                "validation_plan": "recompute" if plan_recompute else "reuse",
                "block_accumulate": "recompute" if accum_recompute else "reuse",
                "aggregate_validate": "recompute" if aggregate_recompute else "reuse",
            }
            for node_name, status in statuses.items():
                node_id = f"{job_id}:{node_name}"
                if status == "recompute":
                    recomputed_nodes.append(node_id)
                else:
                    reused_nodes.append(node_id)
            if any(status == "recompute" for status in statuses.values()):
                affected_jobs.append(job_id)
                recomputed_jobs.append(job_id)
            else:
                fully_reused_jobs.append(job_id)

            job_reports.append(
                {
                    "job_id": job_id,
                    "revision": spec["revision"],
                    "changed_fields": changed_fields,
                    "statuses": statuses,
                    "plan_key": plan_key,
                    "accumulation_key": accum_key,
                    "effective_block_size": item["effective_block_size"],
                    "degradations": item["degradations"],
                    "memory_estimate": item["memory_estimate"],
                }
            )

        recomputed_set = set(recomputed_nodes)
        reused_set = set(reused_nodes)
        all_node_ids = recomputed_set | reused_set
        coverage = {
            "recomputed_count": len(recomputed_nodes),
            "reused_count": len(reused_nodes),
            "intersection": sorted(recomputed_set & reused_set),
            "intersection_empty": not (recomputed_set & reused_set),
            "union_node_count": len(all_node_ids),
            "expected_node_count": len(prepared) * 3,
            "union_covers_all_graph_nodes": len(all_node_ids) == len(prepared) * 3,
            "affected_jobs": sorted(affected_jobs),
            "recomputed_jobs": sorted(recomputed_jobs),
            "fully_reused_jobs": sorted(fully_reused_jobs),
            "affected_jobs_coverable_by_recomputed_nodes": set(affected_jobs) <= set(recomputed_jobs),
        }
        return {
            "schema": "incremental-impact/v1",
            "active_job_count": len(prepared),
            "recomputed_nodes": sorted(recomputed_nodes),
            "reused_nodes": sorted(reused_nodes),
            "affected_jobs": sorted(affected_jobs),
            "recomputed_jobs": sorted(recomputed_jobs),
            "fully_reused_jobs": sorted(fully_reused_jobs),
            "jobs": job_reports,
            "coverage": coverage,
            "parallel_jobs": effective_parallel,
            "degradations": global_degradations,
        }

    def run_pending(self, *, kill_after_chunks: int | None = None) -> dict[str, Any]:
        if not self.pending_path.exists():
            raise WorkbenchError(Codes.NOT_FOUND, "no pending run; submit a manifest first")
        pending = read_json(self.pending_path)
        self.budget = Budget(**pending["budget"]).validated()
        prepared, effective_parallel, _ = self.prepare_specs(
            pending["specs"], parallel_jobs=int(pending.get("parallel_jobs", 1))
        )
        run_id = pending["run_id"]
        next_stage, committed_stage, committed_snapshot = self.snapshots.recovery_position(run_id)
        if committed_stage == "report_export":
            final = self._finalize_outputs(run_id, committed_snapshot, prepared)
            final["recovery"] = {"resumed_after_committed_stage": "report_export", "recomputed_stages": []}
            return final

        artifacts: dict[str, Any] = {}
        if committed_snapshot is not None:
            artifacts = copy.deepcopy(committed_snapshot.get("job_artifacts", {}))
        stage_reports: list[dict[str, Any]] = []

        stage_order = [name for name, _ in STAGES]
        start_index = next_stage + 1 if committed_stage is not None else 1
        for stage_index in range(start_index, len(STAGES) + 1):
            stage_name = stage_order[stage_index - 1]
            staging_dir = self.state_dir / "staging" / run_id / stage_name
            shutil.rmtree(staging_dir, ignore_errors=True)
            staging_dir.mkdir(parents=True, exist_ok=True)
            committed_keys = self.snapshots.committed_cache_keys()
            staged_commit = StagedCacheCommit()
            computed: list[str] = []
            cache_hits: list[str] = []
            cache_reports: dict[str, list[dict[str, Any]]] = {}

            if stage_name == "validation_plan":
                outputs = ordered_parallel(prepared, effective_parallel, lambda item: self._plan_stage_item(item, committed_keys, staged_commit))
                for job_id, artifact, report, pending_object, was_hit in outputs:
                    artifacts[job_id] = artifact
                    cache_reports.setdefault(job_id, []).append(report)
                    (cache_hits if was_hit else computed).append(job_id)
                    if pending_object is not None:
                        staged_commit.add_object(pending_object[0], pending_object[1])

            elif stage_name == "block_accumulate":
                self._fault_chunk_count = 0
                outputs = ordered_parallel(
                    prepared,
                    effective_parallel,
                    lambda item: self._accumulate_stage_item(
                        item,
                        artifacts,
                        committed_keys,
                        staged_commit,
                        staging_dir,
                        kill_after_chunks,
                    ),
                )
                for job_id, node_summary, report, pending_object, pending_blob, was_hit in outputs:
                    artifacts.setdefault(job_id, {"nodes": {}}).setdefault("nodes", {})["block_accumulate"] = node_summary
                    cache_reports.setdefault(job_id, []).append(report)
                    (cache_hits if was_hit else computed).append(job_id)
                    if pending_object is not None:
                        staged_commit.add_object(pending_object[0], pending_object[1])
                    if pending_blob is not None:
                        staged_commit.add_blob(pending_blob[0], pending_blob[1])

            elif stage_name == "aggregate_verify":
                outputs = ordered_parallel(prepared, effective_parallel, lambda item: self._aggregate_stage_item(item, artifacts, committed_keys, staged_commit))
                for job_id, node_summary, report, pending_object, was_hit in outputs:
                    artifacts.setdefault(job_id, {"nodes": {}}).setdefault("nodes", {})["aggregate_validate"] = node_summary
                    artifacts[job_id]["result_hash"] = node_summary["payload_hash"]
                    cache_reports.setdefault(job_id, []).append(report)
                    (cache_hits if was_hit else computed).append(job_id)
                    if pending_object is not None:
                        staged_commit.add_object(pending_object[0], pending_object[1])

            else:
                for item in prepared:
                    job_id = item["spec"]["job_id"]
                    artifacts.setdefault(job_id, {"nodes": {}}).setdefault("nodes", {})["report_export"] = {
                        "stage": "report_export",
                        "report_schema": "job-result/v1",
                    }
                    computed.append(job_id)

            snapshot = self.snapshots.commit_snapshot(
                run_id=run_id,
                stage=stage_name,
                stage_index=stage_index,
                completed_jobs=[item["spec"]["job_id"] for item in prepared],
                job_artifacts=artifacts,
                pre_commit=(lambda: staged_commit.promote(self.cache)) if staged_commit.objects or staged_commit.blobs else None,
            )
            stage_reports.append(
                {
                    "stage": stage_name,
                    "stage_index": stage_index,
                    "snapshot_version": snapshot["version"],
                    "computed_jobs": sorted(computed),
                    "cache_hit_jobs": sorted(cache_hits),
                    "computed_count": len(computed),
                    "cache_hit_count": len(cache_hits),
                }
            )
            all_cache_reports = self._load_cache_reports()
            all_cache_reports.update(cache_reports)
            atomic_write_json(self.cache_report_path, all_cache_reports)

        final_snapshot = self.snapshots.latest_for_run(run_id)
        final = self._finalize_outputs(run_id, final_snapshot, prepared, stage_reports=stage_reports)
        final["recovery"] = {
            "resumed_after_committed_stage": committed_stage,
            "start_index": start_index,
            "recomputed_stages": [report["stage"] for report in stage_reports],
        }
        atomic_write_json(self.state_dir / "execution-report.json", final)
        return final

    def _plan_stage_item(
        self,
        item: dict[str, Any],
        committed_keys: set[str],
        staged_commit: StagedCacheCommit,
    ) -> tuple[str, dict[str, Any], dict[str, Any], tuple[str, dict[str, Any]] | None, bool]:
        spec = item["spec"]
        job_id = spec["job_id"]
        plan = build_plan(
            spec,
            item["array_hashes"],
            effective_block_size=item["effective_block_size"],
            degradations=item["degradations"],
            memory_estimate=item["memory_estimate"],
        )
        parts = plan_key_parts(spec, item["array_hashes"], plan)
        key = make_node_key("validation_plan", parts)
        is_hit = key in committed_keys and self.cache.has_object(key)
        pending_object = None
        payload_hash = plan["plan_hash"]
        if not is_hit:
            pending_object = (key, plan)
        else:
            payload_hash = self.cache.get_object(key)["payload"]["plan_hash"]
        report = cache_key_report("validation_plan", key, parts, is_hit)
        node_summary = {
            "key": key,
            "payload_hash": payload_hash,
            "last_hit": is_hit,
        }
        artifact = {
            "spec_hash": item["spec_hash"],
            "input_hash": item["input_hash"],
            "revision": spec["revision"],
            "effective_block_size": item["effective_block_size"],
            "degradations": item["degradations"],
            "nodes": {"validation_plan": node_summary},
        }
        return job_id, artifact, report, pending_object, is_hit

    def _accumulate_stage_item(
        self,
        item: dict[str, Any],
        artifacts: dict[str, Any],
        committed_keys: set[str],
        staged_commit: StagedCacheCommit,
        staging_dir: Path,
        kill_after_chunks: int | None,
    ) -> tuple[str, dict[str, Any], dict[str, Any], tuple[str, dict[str, Any]] | None, tuple[str, Path] | None, bool]:
        spec = item["spec"]
        job_id = spec["job_id"]
        plan_key = artifacts[job_id]["nodes"]["validation_plan"]["key"]
        plan_payload = self.cache.get_object(plan_key)["payload"]
        parts = accumulation_key_parts(spec, item["array_hashes"], plan_payload)
        key = make_node_key("block_accumulate", parts)
        is_hit = key in committed_keys and self.cache.has_object(key)
        pending_object = None
        pending_blob = None

        if is_hit:
            payload = self.cache.get_object(key)["payload"]
            node_summary = {
                "key": key,
                "payload_hash": payload["accumulation_hash"],
                "output_blob": payload["output_blob"],
                "chunk_count": len(payload.get("chunks", [])),
                "last_hit": True,
            }
        else:
            chunk_dir = staging_dir / "chunks"
            chunk_dir.mkdir(parents=True, exist_ok=True)

            def on_chunk(chunk_id: str, tile: np.ndarray, info: dict[str, Any]) -> dict[str, Any]:
                safe_name = chunk_id.replace(":", "_").replace("/", "_")
                chunk_path = chunk_dir / f"{safe_name}.npy"
                write_npy_atomic(chunk_path, tile)
                if kill_after_chunks is not None:
                    self._fault_chunk_count += 1
                    if self._fault_chunk_count >= int(kill_after_chunks):
                        os.kill(os.getpid(), signal.SIGKILL)
                return {"staged": True}

            computation = tiled_multiply_accumulate(
                spec,
                item["arrays"],
                block_size=item["effective_block_size"],
                on_chunk=on_chunk,
            )
            output = computation["output"]
            output_hash = sha256_json({"array_digest": array_digest_safe(output)})
            from .canonical import array_digest

            output_digest = array_digest(output)["sha256"]
            blob_path = staging_dir / "cache-blobs" / f"{output_digest}.npy"
            write_npy_atomic(blob_path, output)
            pending_blob = (output_digest, blob_path)
            payload = {
                "algorithm": "neumaier-block-fma64/v1",
                "accumulation_hash": output_digest,
                "output_blob": output_digest,
                "chunks": computation["chunks"],
                "loop_order": computation["loop_order"],
                "accumulation_order": computation["accumulation_order"],
                "subnormal_terms_protected": computation["subnormal_terms_protected"],
                "effective_block_size": computation["block_size"],
            }
            pending_object = (key, payload)
            node_summary = {
                "key": key,
                "payload_hash": output_digest,
                "output_blob": output_digest,
                "chunk_count": len(payload["chunks"]),
                "last_hit": False,
            }

        report = cache_key_report("block_accumulate", key, parts, is_hit)
        return job_id, node_summary, report, pending_object, pending_blob, is_hit

    def _aggregate_stage_item(
        self,
        item: dict[str, Any],
        artifacts: dict[str, Any],
        committed_keys: set[str],
        staged_commit: StagedCacheCommit,
    ) -> tuple[str, dict[str, Any], dict[str, Any], tuple[str, dict[str, Any]] | None, bool]:
        spec = item["spec"]
        job_id = spec["job_id"]
        accum_node = artifacts[job_id]["nodes"]["block_accumulate"]
        key = accum_node["key"]
        accum_payload = self.cache.get_object(key)["payload"]
        parts = aggregate_key_parts(spec, item["array_hashes"], accum_payload)
        aggregate_key = make_node_key("aggregate_validate", parts)
        is_hit = aggregate_key in committed_keys and self.cache.has_object(aggregate_key)
        pending_object = None
        if is_hit:
            payload = self.cache.get_object(aggregate_key)["payload"]
        else:
            output = self.cache.load_blob(accum_payload["output_blob"])
            spectrum = verify_against_reference(spec, output, item["arrays"])
            result_hash = accum_payload["accumulation_hash"]
            payload = {
                "algorithm": "reference-verify-spectrum/v1",
                "result_hash": result_hash,
                "spectrum": spectrum,
                "verified": True,
            }
            pending_object = (aggregate_key, payload)
        node_summary = {
            "key": aggregate_key,
            "payload_hash": payload["result_hash"],
            "verified": payload.get("verified", True),
            "last_hit": is_hit,
        }
        report = cache_key_report("aggregate_validate", aggregate_key, parts, is_hit)
        return job_id, node_summary, report, pending_object, is_hit

    def _load_cache_reports(self) -> dict[str, Any]:
        if self.cache_report_path.exists():
            return read_json(self.cache_report_path)
        return {}

    def _finalize_outputs(
        self,
        run_id: str,
        snapshot: dict[str, Any] | None,
        prepared: list[dict[str, Any]],
        *,
        stage_reports: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if snapshot is None or snapshot.get("stage") != "report_export":
            raise WorkbenchError(Codes.STATE_INVALID, "cannot finalize before report_export snapshot")
        artifacts = snapshot.get("job_artifacts", {})
        result_summaries: list[dict[str, Any]] = []
        state_jobs: dict[str, Any] = {}

        for item in prepared:
            spec = item["spec"]
            job_id = spec["job_id"]
            artifact = artifacts[job_id]
            aggregate_node = artifact["nodes"]["aggregate_validate"]
            aggregate_payload = self.cache.get_object(aggregate_node["key"])["payload"]
            accum_node = artifact["nodes"]["block_accumulate"]
            accum_payload = self.cache.get_object(accum_node["key"])["payload"]
            output = self.cache.load_blob(accum_payload["output_blob"])
            natural_output = output[0] if spec["op_kind"] == "gemm" else output
            result_document = {
                "job_id": job_id,
                "op_kind": spec["op_kind"],
                "revision": spec["revision"],
                "dtype": spec["dtype"],
                "layout": spec["layout"],
                "alpha": spec["alpha"],
                "beta": spec["beta"],
                "block_size": spec["block_size"],
                "effective_block_size": item["effective_block_size"],
                "input_hash": item["input_hash"],
                "result_hash": aggregate_payload["result_hash"],
                "output": natural_output.tolist(),
                "spectrum": aggregate_payload["spectrum"],
                "chunks": accum_payload["chunks"],
                "loop_order": accum_payload["loop_order"],
                "accumulation_order": accum_payload["accumulation_order"],
                "subnormal_terms_protected": accum_payload["subnormal_terms_protected"],
                "degradations": item["degradations"],
            }
            atomic_write_json(self.result_dir / f"{job_id}.json", result_document)
            diagnosis = {
                "job_id": job_id,
                "result_hash": aggregate_payload["result_hash"],
                "spectrum": aggregate_payload["spectrum"],
                "graph": {
                    node: {
                        "key": artifact["nodes"].get(node, {}).get("key"),
                        "payload_hash": artifact["nodes"].get(node, {}).get("payload_hash"),
                        "last_hit": artifact["nodes"].get(node, {}).get("last_hit"),
                    }
                    for node in ("validation_plan", "block_accumulate", "aggregate_validate")
                },
            }
            atomic_write_json(self.diagnosis_dir / f"{job_id}.json", diagnosis)
            result_summaries.append({"job_id": job_id, "result_hash": aggregate_payload["result_hash"], "input_hash": item["input_hash"]})
            state_jobs[job_id] = {
                "spec": spec,
                "spec_hash": item["spec_hash"],
                "input_hash": item["input_hash"],
                "array_hashes": item["array_hashes"],
                "revision": spec["revision"],
                "effective_block_size": item["effective_block_size"],
                "degradations": item["degradations"],
                "result_hash": aggregate_payload["result_hash"],
                "result_file": f"results/{job_id}.json",
                "nodes": artifact["nodes"],
            }

        logical_ms = BASE_LOGICAL_MS + int(snapshot["version"]) * 1000
        v1, v2 = self.ledger.upsert_results(result_summaries, logical_ms=logical_ms)
        committed_state = {
            "version": 1,
            "run_id": run_id,
            "latest_snapshot_version": snapshot["version"],
            "jobs": state_jobs,
        }
        atomic_write_json(self.committed_state_path, committed_state)
        overview = {
            "run_id": run_id,
            "completed": True,
            "job_count": len(prepared),
            "job_ids": [item["spec"]["job_id"] for item in prepared],
            "latest_snapshot_version": snapshot["version"],
            "ledger": {"v1_rows": v1["row_count"], "v2_records": v2["record_count"]},
            "stages": [{"stage": name, "label": label} for name, label in STAGES],
        }
        atomic_write_json(self.state_dir / "overview.json", overview)
        final = {
            "ok": True,
            "run_id": run_id,
            "completed": True,
            "job_count": len(prepared),
            "job_ids": overview["job_ids"],
            "snapshot_version": snapshot["version"],
            "stage_reports": stage_reports or [],
            "ledger": overview["ledger"],
            "result_hashes": {item["job_id"]: item["result_hash"] for item in result_summaries},
        }
        return final

    def query_cache(self, job_id: str) -> dict[str, Any]:
        state = self.load_committed_state()
        job = state.get("jobs", {}).get(job_id)
        if job is None:
            raise WorkbenchError(Codes.NOT_FOUND, f"job {job_id!r} not found in committed state")
        spec = job["spec"]
        hashes = job["array_hashes"]
        nodes: dict[str, Any] = {}
        plan_key = job["nodes"]["validation_plan"]["key"]
        plan_payload = self.cache.get_object(plan_key)["payload"]
        plan_parts = plan_key_parts(spec, hashes, plan_payload)
        nodes["validation_plan"] = {
            "key": plan_key,
            "object_exists": self.cache.has_object(plan_key),
            "committed": True,
            "last_hit": job["nodes"]["validation_plan"].get("last_hit"),
            "key_report": cache_key_report("validation_plan", plan_key, plan_parts, bool(job["nodes"]["validation_plan"].get("last_hit"))),
        }
        accum_key = job["nodes"]["block_accumulate"]["key"]
        accum_payload = self.cache.get_object(accum_key)["payload"]
        accum_parts = accumulation_key_parts(spec, hashes, plan_payload)
        nodes["block_accumulate"] = {
            "key": accum_key,
            "object_exists": self.cache.has_object(accum_key),
            "committed": True,
            "last_hit": job["nodes"]["block_accumulate"].get("last_hit"),
            "key_report": cache_key_report("block_accumulate", accum_key, accum_parts, bool(job["nodes"]["block_accumulate"].get("last_hit"))),
        }
        aggregate_key = job["nodes"]["aggregate_validate"]["key"]
        aggregate_payload = self.cache.get_object(aggregate_key)["payload"]
        aggregate_parts = aggregate_key_parts(spec, hashes, accum_payload)
        nodes["aggregate_validate"] = {
            "key": aggregate_key,
            "object_exists": self.cache.has_object(aggregate_key),
            "committed": True,
            "last_hit": job["nodes"]["aggregate_validate"].get("last_hit"),
            "key_report": cache_key_report("aggregate_validate", aggregate_key, aggregate_parts, bool(job["nodes"]["aggregate_validate"].get("last_hit"))),
        }
        return {"job_id": job_id, "input_hash": job["input_hash"], "nodes": nodes}


def read_json_line(line: str) -> dict[str, Any]:
    import json

    return json.loads(line)


def array_digest_safe(output: np.ndarray) -> dict[str, Any]:
    from .canonical import array_digest

    return array_digest(output)


def ordered_parallel(items: list[dict[str, Any]], workers: int, function: Any) -> list[Any]:
    if workers == 1:
        return [function(item) for item in items]
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(function, item) for item in items]
        return [future.result() for future in futures]


def execute_records(
    records: list[dict[str, Any]],
    state_dir: str | Path,
    *,
    budget: Budget | dict[str, Any] | None = None,
    parallel_jobs: int = 1,
) -> dict[str, Any]:
    bench = Workbench(state_dir, budget=budget)
    return bench.submit_records(records, parallel_jobs=parallel_jobs)
