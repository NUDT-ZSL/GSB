from __future__ import annotations

import json
import os
import shutil
import signal
from pathlib import Path
from typing import Any

from .cache import ArtifactCache, build_cache_key
from .checkpoint import CheckpointManager
from .chunked import run_chunked
from .contracts import parameter_digest, semantic_parameters
from .engine import evaluate_job
from .errors import OpVerifyError
from .hashing import array_content_hash, canonical_bytes, sha256_json
from .ledger import LedgerStore
from .planner import build_plan
from .reference import reference_task
from .samples import load_arrays, read_jsonl
from .snapshots import SnapshotStore, make_envelope
from .verify import compare_outputs, declared_tolerance


COMPUTE_STAGES = ("input", "reference", "plan", "chunk")


class VerificationService:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.cache = ArtifactCache(self.root / "cas")
        self.ledger = LedgerStore(self.root)
        self.snapshots = SnapshotStore(self.root)
        self.checkpoints = CheckpointManager(self.root)
        self.tmp_root = self.root / "tmp"
        self.events_path = self.root / "events.jsonl"
        self.tmp_root.mkdir(parents=True, exist_ok=True)
        self._recover_snapshot_pointers()

    def _event(self, event: str, **fields: Any) -> None:
        payload = {"event": event, **fields}
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def _recover_snapshot_pointers(self) -> None:
        version = self.ledger.current_version()
        if version == 0:
            return
        current = self.snapshots.load_current("ledger")
        if current is None or int(current["version"]) < version:
            self.snapshots.promote_version(version)

    def _reset_temp(self, batch_id: str) -> Path:
        safe = batch_id.replace("/", "_").replace("..", "_")
        temp_dir = self.tmp_root / safe
        shutil.rmtree(temp_dir, ignore_errors=True)
        temp_dir.mkdir(parents=True, exist_ok=True)
        return temp_dir

    def run_manifest(
        self,
        manifest_path: str | Path,
        *,
        batch_id: str | None = None,
        crash_after_commits: int | None = None,
        crash_stage: str = "chunk",
    ) -> dict[str, Any]:
        path = Path(manifest_path).expanduser().resolve()
        records = read_jsonl(path)
        base_dir = path.parent
        batch = batch_id or path.stem
        try:
            return self.run_records(
                records,
                batch_id=batch,
                base_dir=base_dir,
                crash_after_commits=crash_after_commits,
                crash_stage=crash_stage,
            )
        except OpVerifyError as exc:
            self._event("rejected", batch_id=batch, error=exc.code, details=exc.to_dict())
            return exc.to_dict()

    def run_records(
        self,
        records: list[dict[str, Any]],
        *,
        batch_id: str,
        base_dir: str | Path | None = None,
        crash_after_commits: int | None = None,
        crash_stage: str = "chunk",
    ) -> dict[str, Any]:
        base = Path(base_dir) if base_dir is not None else None
        if base is not None:
            self.ledger.set_data_dir(base)
        parent_version = self.ledger.current_version()
        heads = self.ledger.heads()

        incoming: list[dict[str, Any]] = []
        for raw in records:
            job_id = str(raw.get("job_id", "unknown"))
            head = heads.get(job_id)
            prior_revision = int(head["revision"]) if head else None
            from .contracts import normalize_task

            task = normalize_task(raw, prior_revision=prior_revision)
            if head is not None and task["revision"] == int(head["revision"]):
                if canonical_bytes(task) != canonical_bytes(head["task"]):
                    raise OpVerifyError(
                        "E_REVISION_CONFLICT",
                        "same revision was resubmitted with different parameters",
                        job_id=job_id,
                        field="$.revision",
                        details={"revision": task["revision"]},
                    )
            incoming.append({"raw": raw, "task": task, "head": head})

        if incoming and all(
            item["head"] is not None
            and item["task"]["revision"] == int(item["head"]["revision"])
            and canonical_bytes(item["task"]) == canonical_bytes(item["head"]["task"])
            for item in incoming
        ):
            record = self.ledger.latest_version_record()
            self._event("idempotent_reuse", batch_id=batch_id, version=parent_version)
            return {
                "ok": True,
                "idempotent": True,
                "batch_id": batch_id,
                "ledger_version": parent_version,
                "change_plan": record["change_plan"] if record else None,
            }

        tasks = {job_id: head["task"] for job_id, head in heads.items()}
        for item in incoming:
            tasks[item["task"]["job_id"]] = item["task"]

        for job_id, task in tasks.items():
            for dependency in task.get("depends_on", []):
                if dependency not in tasks:
                    raise OpVerifyError(
                        "E_INCONSISTENT",
                        f"job {job_id} depends on missing job {dependency}",
                        job_id=job_id,
                        field="$.depends_on",
                    )

        dependents: dict[str, list[str]] = {job_id: [] for job_id in tasks}
        for job_id, task in tasks.items():
            for dependency in task.get("depends_on", []):
                dependents.setdefault(dependency, []).append(job_id)

        target_version = parent_version + 1
        plan = self._build_change_plan(
            batch_id=batch_id,
            target_version=target_version,
            parent_version=parent_version,
            incoming=incoming,
            tasks=tasks,
            heads=heads,
            dependents=dependents,
            base_dir=base,
        )
        self._event(
            "plan_built",
            batch_id=batch_id,
            target_version=target_version,
            recompute_jobs=plan["recompute_jobs"],
            reused_jobs=plan["fully_reused_jobs"],
        )
        report = self._execute_plan(
            plan=plan,
            tasks=tasks,
            heads=heads,
            base_dir=base,
            crash_after_commits=crash_after_commits,
            crash_stage=crash_stage,
        )
        return report

    def _category_stages(self, category: str) -> list[str]:
        if category in ("new", "shape", "input_content"):
            return list(COMPUTE_STAGES)
        if category in ("geometry", "core"):
            return ["reference", "plan", "chunk"]
        if category == "block_policy":
            return ["plan", "chunk"]
        if category in ("revision_only", "downstream"):
            return ["chunk"]
        return ["chunk"]

    def _build_change_plan(
        self,
        *,
        batch_id: str,
        target_version: int,
        parent_version: int,
        incoming: list[dict[str, Any]],
        tasks: dict[str, dict[str, Any]],
        heads: dict[str, dict[str, Any]],
        dependents: dict[str, list[str]],
        base_dir: Path | None,
    ) -> dict[str, Any]:
        changes: dict[str, dict[str, Any]] = {}
        root_ids: list[str] = []
        trigger_revisions: dict[str, int] = {}

        for item in incoming:
            task = item["task"]
            head = item["head"]
            job_id = task["job_id"]
            root_ids.append(job_id)
            trigger_revisions[job_id] = int(task["revision"])
            arrays = load_arrays(task, base_dir)
            input_hash = array_content_hash(arrays["input"])

            if head is None:
                category = "new"
                changed_fields = ["job"]
                reason = "job has no committed ledger row"
            else:
                old_task = head["task"]
                changed_fields = sorted(
                    key
                    for key in set(semantic_parameters(task)) | set(semantic_parameters(old_task))
                    if semantic_parameters(task).get(key) != semantic_parameters(old_task).get(key)
                )
                old_policy = canonical_bytes(old_task.get("block_policy"))
                new_policy = canonical_bytes(task.get("block_policy"))
                policy_changed = old_policy != new_policy

                if task["input_shape"] != old_task["input_shape"]:
                    category = "shape"
                    if "input_shape" not in changed_fields:
                        changed_fields.append("input_shape")
                    reason = "input_shape changes every stage and output row"
                elif input_hash != head.get("input_hash"):
                    category = "input_content"
                    changed_fields.append("input_hash")
                    reason = "binary input content changed"
                elif task["stride"] != old_task["stride"] or task["padding"] != old_task["padding"]:
                    category = "geometry"
                    for field_name in ("stride", "padding"):
                        if task[field_name] != old_task[field_name]:
                            changed_fields.append(field_name)
                    reason = "stride/padding changes operator geometry/core"
                elif policy_changed and not changed_fields:
                    category = "block_policy"
                    changed_fields = ["block_policy"]
                    reason = "only block_policy changed; numerical reference is stable"
                elif changed_fields or parameter_digest(task) != head.get("parameter_digest"):
                    category = "core"
                    reason = "numerical operator parameter changed"
                else:
                    category = "revision_only"
                    changed_fields = ["revision"]
                    reason = "revision binding changed while parameters are unchanged"

            stages = self._category_stages(category)
            changes[job_id] = {
                "category": category,
                "changed_fields": sorted(set(changed_fields)),
                "recompute_stages": stages,
                "reused_stages": [stage for stage in COMPUTE_STAGES if stage not in stages],
                "reason": reason,
                "new_revision": int(task["revision"]),
                "input_hash_preview": input_hash,
            }

        propagation: dict[str, dict[str, Any]] = {}
        observed_dependency_jobs: set[str] = set()
        for root_id in root_ids:
            root_category = changes[root_id]["category"]
            queue: list[tuple[str, str]] = [(child, root_id) for child in dependents.get(root_id, [])]
            visited: set[str] = set()
            while queue:
                job_id, because = queue.pop(0)
                if job_id in visited:
                    continue
                visited.add(job_id)
                observed_dependency_jobs.add(job_id)
                if root_category == "block_policy":
                    propagation.setdefault(
                        job_id,
                        {
                            "recompute_stages": set(),
                            "reused_stages": set(COMPUTE_STAGES),
                            "reasons": set(),
                            "stable_output": True,
                        },
                    )
                    propagation[job_id]["reasons"].add(
                        f"{because}:block_policy_does_not_change_output_digest"
                    )
                else:
                    entry = propagation.setdefault(
                        job_id,
                        {
                            "recompute_stages": set(),
                            "reused_stages": set(COMPUTE_STAGES),
                            "reasons": set(),
                            "stable_output": False,
                        },
                    )
                    entry["recompute_stages"].add("chunk")
                    entry["reused_stages"].discard("chunk")
                    entry["reasons"].add(f"{because}:upstream_artifact_hash_changes")
                for child in dependents.get(job_id, []):
                    queue.append((child, job_id))

        job_stages: dict[str, list[str]] = {}
        job_reuse_stages: dict[str, list[str]] = {}
        propagation_summary: dict[str, Any] = {}
        for job_id in tasks:
            if job_id in changes:
                job_stages[job_id] = list(changes[job_id]["recompute_stages"])
                job_reuse_stages[job_id] = list(changes[job_id]["reused_stages"])
            elif job_id in propagation:
                stages = sorted(propagation[job_id]["recompute_stages"], key=COMPUTE_STAGES.index)
                job_stages[job_id] = stages
                job_reuse_stages[job_id] = [
                    stage for stage in COMPUTE_STAGES if stage not in stages
                ]
                propagation_summary[job_id] = {
                    "recompute_stages": stages,
                    "reused_stages": job_reuse_stages[job_id],
                    "reasons": sorted(propagation[job_id]["reasons"]),
                }
            else:
                job_stages[job_id] = []
                job_reuse_stages[job_id] = list(COMPUTE_STAGES) + ["ledger_row"]

        order = self._topological_order(tasks)
        active_order = [job_id for job_id in order if job_stages[job_id]]
        recompute_entries = [
            {"job_id": job_id, "stages": job_stages[job_id]}
            for job_id in order
            if job_stages[job_id]
        ]
        reused_entries = [
            {"job_id": job_id, "stages": job_reuse_stages[job_id]}
            for job_id in order
            if job_reuse_stages[job_id]
        ]
        recompute_jobs = sorted({entry["job_id"] for entry in recompute_entries})
        reused_jobs = sorted({entry["job_id"] for entry in reused_entries})
        fully_reused_jobs = sorted(set(tasks) - set(recompute_jobs))
        affected_jobs = sorted(set(root_ids) | observed_dependency_jobs)

        return {
            "schema": "opverify-change-plan-v1",
            "batch_id": batch_id,
            "target_version": target_version,
            "parent_version": parent_version,
            "trigger_revision": {
                "revisions": trigger_revisions,
                "max_revision": max(trigger_revisions.values()),
            },
            "changes": changes,
            "propagation": propagation_summary,
            "job_stages": job_stages,
            "job_reuse_stages": job_reuse_stages,
            "order": order,
            "active_order": active_order,
            "affected_jobs": affected_jobs,
            "recompute": recompute_entries,
            "reused": reused_entries,
            "recompute_jobs": recompute_jobs,
            "reused_jobs": reused_jobs,
            "fully_reused_jobs": fully_reused_jobs,
        }

    def _topological_order(self, tasks: dict[str, dict[str, Any]]) -> list[str]:
        visited: set[str] = set()
        visiting: set[str] = set()
        order: list[str] = []

        def visit(job_id: str) -> None:
            if job_id in visited:
                return
            if job_id in visiting:
                raise OpVerifyError(
                    "E_INCONSISTENT", f"dependency cycle at {job_id}", job_id=job_id
                )
            visiting.add(job_id)
            for dependency in tasks[job_id].get("depends_on", []):
                visit(dependency)
            visiting.remove(job_id)
            visited.add(job_id)
            order.append(job_id)

        for job_id in sorted(tasks):
            visit(job_id)
        return order

    def _execute_plan(
        self,
        *,
        plan: dict[str, Any],
        tasks: dict[str, dict[str, Any]],
        heads: dict[str, dict[str, Any]],
        base_dir: Path | None,
        crash_after_commits: int | None,
        crash_stage: str,
    ) -> dict[str, Any]:
        batch_id = plan["batch_id"]
        target_version = int(plan["target_version"])
        temp_dir = self._reset_temp(batch_id)
        states: dict[str, dict[str, Any]] = {}
        execution_report: dict[str, Any] = {
            "computed": [],
            "skipped_committed": [],
            "checkpoints": [],
        }
        new_commit_count = 0

        for job_id in plan["active_order"]:
            head = heads.get(job_id)
            states[job_id] = {
                "task": tasks[job_id],
                "stages": plan["job_stages"][job_id],
                "input_hash": head.get("input_hash") if head else None,
                "weight_hash": head.get("weight_hash") if head else None,
                "reference_digest": head.get("reference_digest") if head else None,
                "plan": head.get("plan") if head else None,
                "output_digest": head.get("output_digest") if head else None,
                "cache_key": head.get("cache_key") if head else None,
                "job_row": None,
                "arrays": None,
                "reference": None,
                "plan_object": None,
                "output": None,
            }

        for stage in COMPUTE_STAGES:
            stage_jobs = [
                job_id
                for job_id in plan["active_order"]
                if stage in plan["job_stages"][job_id]
            ]
            checksums: dict[str, str] = {}
            for job_id in stage_jobs:
                existing = self.ledger.stage_commit(batch_id, target_version, job_id, stage)
                if existing is not None:
                    digest = str(existing["digest"])
                    self._restore_stage(stage, states[job_id], existing)
                    execution_report["skipped_committed"].append(
                        {"job_id": job_id, "stage": stage, "digest": digest}
                    )
                    self._event(
                        "skip_committed_job",
                        batch_id=batch_id,
                        job_id=job_id,
                        stage=stage,
                    )
                else:
                    digest, payload = self._run_stage(
                        stage=stage,
                        job_id=job_id,
                        state=states[job_id],
                        states=states,
                        base_dir=base_dir,
                        batch_id=batch_id,
                        target_version=target_version,
                        temp_dir=temp_dir,
                    )
                    self.ledger.record_stage_commit(
                        batch_id=batch_id,
                        target_version=target_version,
                        job_id=job_id,
                        stage=stage,
                        digest=digest,
                        payload=payload,
                    )
                    execution_report["computed"].append(
                        {"job_id": job_id, "stage": stage, "digest": digest}
                    )
                    self._event(
                        "stage_committed",
                        batch_id=batch_id,
                        job_id=job_id,
                        stage=stage,
                        digest=digest,
                    )
                    if crash_stage == stage and crash_after_commits is not None:
                        new_commit_count += 1
                        if new_commit_count >= int(crash_after_commits):
                            self._event(
                                "simulated_sigkill",
                                batch_id=batch_id,
                                job_id=job_id,
                                stage=stage,
                                committed=new_commit_count,
                                pid=os.getpid(),
                            )
                            os.kill(os.getpid(), signal.SIGKILL)
                checksums[job_id] = digest

            checkpoint = self.checkpoints.save_stage(
                batch_id=batch_id,
                target_version=target_version,
                stage=stage,
                completed_jobs=stage_jobs,
                job_checksums=checksums,
            )
            execution_report["checkpoints"].append(
                {"stage": stage, "checksum": checkpoint["checksum"], "jobs": stage_jobs}
            )
            self._event("stage_checkpoint", batch_id=batch_id, stage=stage)

        parent_version = int(plan["parent_version"])
        with self.ledger.exclusive_commit():
            current_version = self.ledger.current_version()
            if current_version != parent_version:
                raise OpVerifyError(
                    "E_STAGE_BUSY",
                    "ledger advanced while job was computing; commit rejected",
                    details={
                        "expected_parent": parent_version,
                        "current_version": current_version,
                    },
                )
            version = current_version + 1
            job_rows = self._build_version_rows(
                plan=plan, states=states, heads=heads, version=version
            )
            envelopes = self._build_snapshot_envelopes(
                plan=plan,
                job_rows=job_rows,
                version=version,
                parent_version=parent_version,
                execution_report=execution_report,
            )
            for envelope in envelopes.values():
                self.snapshots.write_versioned(envelope)
            self.ledger.commit_version(
                version=version,
                parent_version=parent_version,
                batch_id=batch_id,
                trigger_revision=plan["trigger_revision"],
                change_plan={**plan, "execution": execution_report},
                snapshot_checksums={name: env["checksum"] for name, env in envelopes.items()},
                job_rows=job_rows,
            )
            for envelope in envelopes.values():
                self.snapshots.replace_current(envelope)
            self.checkpoints.save_stage(
                batch_id=batch_id,
                target_version=version,
                stage="commit",
                completed_jobs=[row["job_id"] for row in job_rows],
                job_checksums={row["job_id"]: row["row_checksum"] for row in job_rows},
            )

        self._event("ledger_committed", batch_id=batch_id, version=version)
        return {
            "ok": True,
            "batch_id": batch_id,
            "ledger_version": version,
            "parent_version": parent_version,
            "trigger_revision": plan["trigger_revision"],
            "affected_jobs": plan["affected_jobs"],
            "recompute": plan["recompute"],
            "reused": plan["reused"],
            "recompute_jobs": plan["recompute_jobs"],
            "reused_jobs": plan["reused_jobs"],
            "fully_reused_jobs": plan["fully_reused_jobs"],
            "propagation": plan["propagation"],
            "execution": execution_report,
            "snapshot_checksums": {name: env["checksum"] for name, env in envelopes.items()},
            "ledger_checksum": envelopes["ledger"]["checksum"],
        }

    def _ensure_arrays(
        self, job_id: str, state: dict[str, Any], base_dir: Path | None
    ) -> dict[str, Any]:
        if state["arrays"] is None:
            state["arrays"] = load_arrays(state["task"], base_dir)
        return state["arrays"]

    def _ensure_reference(self, job_id: str, state: dict[str, Any]) -> Any:
        if state["reference"] is not None:
            return state["reference"]
        digest = state["reference_digest"]
        if not digest:
            raise OpVerifyError("E_ARTIFACT_MISSING", "reference artifact is not committed", job_id=job_id)
        state["reference"] = self.cache.get_array_artifact(digest)
        return state["reference"]

    def _run_stage(
        self,
        *,
        stage: str,
        job_id: str,
        state: dict[str, Any],
        states: dict[str, dict[str, Any]],
        base_dir: Path | None,
        batch_id: str,
        target_version: int,
        temp_dir: Path,
    ) -> tuple[str, dict[str, Any]]:
        task = state["task"]
        if stage == "input":
            arrays = self._ensure_arrays(job_id, state, base_dir)
            state["input_hash"] = array_content_hash(arrays["input"])
            state["weight_hash"] = (
                array_content_hash(arrays["weight"]) if "weight" in arrays else None
            )
            return state["input_hash"], {
                "input_hash": state["input_hash"],
                "weight_hash": state["weight_hash"],
            }

        arrays = self._ensure_arrays(job_id, state, base_dir)

        if stage == "reference":
            reference = reference_task(task, arrays)
            digest, manifest = self.cache.put_array_artifact(reference)
            state["reference"] = reference
            state["reference_digest"] = digest
            return digest, {"reference_digest": digest, "manifest": manifest}

        if stage == "plan":
            plan = build_plan(task)
            state["plan_object"] = plan
            state["plan"] = plan
            return str(plan["plan_digest"]), {"plan": plan}

        if stage != "chunk":
            raise OpVerifyError("E_INCONSISTENT", f"unknown stage {stage}", job_id=job_id)

        reference = self._ensure_reference(job_id, state)
        plan = state.get("plan_object") or state.get("plan") or build_plan(task)
        job_temp = temp_dir / job_id
        output = run_chunked(task, arrays, plan=plan, temp_dir=job_temp)
        tolerance = declared_tolerance(task)
        comparison = compare_outputs(reference, output, **tolerance)
        output_digest, output_manifest = self.cache.put_array_artifact(output)
        state["output"] = output
        state["output_digest"] = output_digest

        upstream_artifacts: list[dict[str, str]] = []
        for dependency in task.get("depends_on", []):
            dependency_state = states.get(dependency)
            if dependency_state is not None and dependency_state.get("output_digest"):
                artifact_hash = dependency_state["output_digest"]
            else:
                head = self.ledger.get_head(dependency)
                if head is None:
                    raise OpVerifyError(
                        "E_ARTIFACT_MISSING",
                        f"upstream {dependency} has no committed artifact",
                        job_id=job_id,
                    )
                artifact_hash = head["output_digest"]
            upstream_artifacts.append({"job_id": dependency, "artifact_hash": artifact_hash})

        cache_key, cache_payload = build_cache_key(
            task,
            input_hash=state["input_hash"] or array_content_hash(arrays["input"]),
            parameter_hash=parameter_digest(task),
            upstream_artifacts=upstream_artifacts,
            weight_hash=state.get("weight_hash"),
        )
        self.cache.put_entry(
            cache_key,
            {
                "cache_key": cache_key,
                "output_digest": output_digest,
                "reference_digest": state["reference_digest"],
                "revision": task["revision"],
                "cache_payload": cache_payload,
            },
        )
        state["cache_key"] = cache_key

        row_without_checksum = {
            "job_id": job_id,
            "revision": int(task["revision"]),
            "ledger_version": target_version,
            "op_kind": task["op_kind"],
            "interp_mode": task.get("interp_mode"),
            "input_hash": state["input_hash"],
            "weight_hash": state.get("weight_hash"),
            "parameter_digest": parameter_digest(task),
            "block_policy": task["block_policy"],
            "cache_key": cache_key,
            "reference_digest": state["reference_digest"],
            "output_digest": output_digest,
            "plan_digest": plan["plan_digest"],
            "plan": plan,
            "output_shape": task["output_shape"],
            "dtype": task["dtype"],
            "upstream_artifacts": upstream_artifacts,
            "comparison": comparison,
            "output_manifest": output_manifest,
            "task": task,
            "parameter_snapshot": semantic_parameters(task),
        }
        row = {
            **row_without_checksum,
            "row_checksum": sha256_json(row_without_checksum),
        }
        state["job_row"] = row
        return output_digest, {"output_digest": output_digest, "cache_key": cache_key, "job_row": row}

    def _restore_stage(
        self, stage: str, state: dict[str, Any], payload: dict[str, Any]
    ) -> None:
        if stage == "input":
            state["input_hash"] = payload.get("input_hash")
            state["weight_hash"] = payload.get("weight_hash")
        elif stage == "reference":
            state["reference_digest"] = payload.get("reference_digest") or payload.get("digest")
        elif stage == "plan":
            plan = payload.get("plan")
            state["plan"] = plan
            state["plan_object"] = plan
        elif stage == "chunk":
            row = payload.get("job_row")
            if row:
                state["job_row"] = row
                state["output_digest"] = row["output_digest"]
                state["cache_key"] = row["cache_key"]

    def _build_version_rows(
        self,
        *,
        plan: dict[str, Any],
        states: dict[str, dict[str, Any]],
        heads: dict[str, dict[str, Any]],
        version: int,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for job_id in plan["order"]:
            state = states.get(job_id)
            if state is not None and state.get("job_row") is not None:
                row = dict(state["job_row"])
                row["ledger_version"] = version
                row.pop("row_checksum", None)
                row["row_checksum"] = sha256_json(
                    {key: value for key, value in row.items() if key != "row_checksum"}
                )
                rows.append(row)
                continue

            head = heads[job_id]
            row = {key: value for key, value in head.items() if key != "row_checksum"}
            row["ledger_version"] = version
            row["row_checksum"] = sha256_json(row)
            rows.append(row)
        return rows

    def _build_snapshot_envelopes(
        self,
        *,
        plan: dict[str, Any],
        job_rows: list[dict[str, Any]],
        version: int,
        parent_version: int,
        execution_report: dict[str, Any],
    ) -> dict[str, Any]:
        ledger_jobs = [
            {
                "job_id": row["job_id"],
                "revision": row["revision"],
                "row_checksum": row["row_checksum"],
                "cache_key": row["cache_key"],
                "output_digest": row["output_digest"],
                "reference_digest": row["reference_digest"],
                "parameter_digest": row["parameter_digest"],
            }
            for row in job_rows
        ]
        ledger_envelope = make_envelope(
            "ledger",
            version,
            {
                "batch_id": plan["batch_id"],
                "parent_version": parent_version,
                "trigger_revision": plan["trigger_revision"],
                "delta": {
                    "schema": "opverify-delta-v1",
                    "affected_jobs": plan["affected_jobs"],
                    "recompute": plan["recompute"],
                    "reused": plan["reused"],
                    "recompute_jobs": plan["recompute_jobs"],
                    "reused_jobs": plan["reused_jobs"],
                    "fully_reused_jobs": plan["fully_reused_jobs"],
                    "propagation": plan["propagation"],
                },
                "jobs": ledger_jobs,
            },
            parent_version=parent_version,
        )
        outputs_envelope = make_envelope(
            "outputs",
            version,
            [
                {
                    "job_id": row["job_id"],
                    "version": version,
                    "revision": row["revision"],
                    "output_digest": row["output_digest"],
                    "reference_digest": row["reference_digest"],
                    "output_shape": row["output_shape"],
                    "dtype": row["dtype"],
                    "comparison": row["comparison"],
                }
                for row in job_rows
            ],
            parent_version=parent_version,
        )
        parameters_envelope = make_envelope(
            "parameters",
            version,
            [
                {
                    "job_id": row["job_id"],
                    "version": version,
                    "revision": row["revision"],
                    "parameter_digest": row["parameter_digest"],
                    "block_policy": row["block_policy"],
                    "parameter_snapshot": row["parameter_snapshot"],
                    "task_snapshot": row["task"],
                }
                for row in job_rows
            ],
            parent_version=parent_version,
        )
        return {
            "ledger": ledger_envelope,
            "outputs": outputs_envelope,
            "parameters": parameters_envelope,
        }

    def standalone_digest(self, job_id: str, base_dir: Path | None = None) -> str:
        head = self.ledger.get_head(job_id)
        if head is None:
            raise OpVerifyError("E_ARTIFACT_MISSING", "job not in ledger", job_id=job_id)
        result = evaluate_job(head["task"], base_dir=base_dir)
        artifact_digest, _ = self.cache.put_array_artifact(result["output"])
        return artifact_digest
