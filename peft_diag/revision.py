"""Controlled task revision transactions and dependency-suffix invalidation plans."""

from __future__ import annotations

import fcntl
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .canonical import (
    atomic_write_bytes,
    canonical_bytes,
    content_hash,
)
from .catalog import Catalog
from .config import DegradationLog, ResourceLimits
from .errors import (
    format_error,
    stage_busy,
    stale_result,
)
from .snapshots import detect_format, load_snapshot, write_v1, write_v2
from .tasks import (
    NormalizedTask,
    normalize_task,
    parse_tasks,
    validate_revision_transition,
    write_tasks,
)
from .training import ALGORITHM_VERSION, BETA1, BETA2


INIT_FIELDS = {
    "revision",
    "base_model_rev",
    "adapter_type",
    "adapter_dim",
    "num_layers",
    "seed",
}
GRAD_FIELDS = {"steps", "micro_batch", "seed"}
UPDATE_FIELDS = {"lr"}


@dataclass
class RevisionRequest:
    case_id: str
    revision: int | None = None
    lr: float | None = None
    adapter_dim: int | None = None
    steps: int | None = None
    micro_batch: int | None = None
    seed: int | None = None
    snapshot_path: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "revision": self.revision,
            "lr": self.lr,
            "adapter_dim": self.adapter_dim,
            "steps": self.steps,
            "micro_batch": self.micro_batch,
            "seed": self.seed,
            "snapshot_path": self.snapshot_path,
        }


class RevisionLock:
    def __init__(self, state_dir: str | Path) -> None:
        self.path = Path(state_dir) / "revision.lock"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fd: int | None = None

    def __enter__(self) -> "RevisionLock":
        self._fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self._fd)
            self._fd = None
            raise stage_busy(
                "another revision transaction is already in progress",
                details={"lock": str(self.path)},
            )
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None


def _impacted_stages(fields: set[str], *, lr_changed: bool, steps_changed: bool) -> list[str]:
    if fields & INIT_FIELDS:
        return ["INITIALIZE", "GRAD_ACCUM", "OPTIMIZER_UPDATE", "EXPORT"]
    if steps_changed:
        return ["GRAD_ACCUM", "OPTIMIZER_UPDATE", "EXPORT"]
    if lr_changed:
        return ["OPTIMIZER_UPDATE", "GRAD_ACCUM:subsequent", "EXPORT"]
    return []


def _cache_keys_for(task: NormalizedTask, snapshot_hash: str) -> dict[str, str]:
    init_key = content_hash(
        {
            "stage": "INITIALIZE",
            "algorithm_version": ALGORITHM_VERSION,
            "task_id": task.case_id,
            "base_model_rev": task.base_model_rev,
            "adapter_type": task.adapter_type,
            "adapter_dim": task.adapter_dim,
            "num_layers": task.num_layers,
            "seed": task.seed,
            "revision": task.revision,
            "snapshot_semantic_hash": snapshot_hash,
            "structure_summary": task.parameter_summary,
        }
    )
    grad_key = content_hash(
        {
            "stage": "GRAD_ACCUM",
            "algorithm_version": ALGORITHM_VERSION,
            "task_id": task.case_id,
            "seed": task.seed,
            "micro_batch": task.micro_batch,
            "update_cycle": 1,
            "previous_update_key": init_key,
        }
    )
    update_key = content_hash(
        {
            "stage": "OPTIMIZER_UPDATE",
            "algorithm_version": ALGORITHM_VERSION,
            "task_id": task.case_id,
            "lr": task.lr,
            "beta1": BETA1,
            "beta2": BETA2,
            "update_cycle": 1,
            "gradient_key": grad_key,
        }
    )
    return {
        "INIT": init_key,
        "GRAD:1": grad_key,
        "UPDATE:1": update_key,
    }


class RevisionService:
    def __init__(
        self,
        *,
        tasks_path: str | Path,
        catalog_path: str | Path,
        state_dir: str | Path,
        limits: ResourceLimits | None = None,
    ) -> None:
        self.tasks_path = Path(tasks_path)
        self.catalog_path = Path(catalog_path)
        self.state_dir = Path(state_dir)
        self.limits = limits or ResourceLimits()
        self.limits.validate()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.degradation = DegradationLog()

    def _build_updated_task(
        self,
        current: NormalizedTask,
        request: RevisionRequest,
    ) -> NormalizedTask:
        if request.case_id != current.case_id:
            raise format_error(
                "revision request case_id does not match task",
                task_id=current.case_id,
                field="case_id",
            )
        raw = current.canonical_fields()
        if request.revision is not None:
            raw["revision"] = request.revision
        if request.lr is not None:
            raw["lr"] = request.lr
        if request.adapter_dim is not None:
            raw["adapter_dim"] = request.adapter_dim
        if request.steps is not None:
            raw["steps"] = request.steps
        if request.micro_batch is not None:
            raw["micro_batch"] = request.micro_batch
        if request.seed is not None:
            raw["seed"] = request.seed
        if request.revision is not None and request.revision < current.revision:
            raise format_error(
                "revision rollback is not allowed",
                task_id=current.case_id,
                field="revision",
                details={"current": current.revision, "requested": request.revision},
            )
        updated = normalize_task(raw)
        changed_fields = {
            field_name
            for field_name in raw
            if field_name in current.canonical_fields()
            and raw[field_name] != current.canonical_fields()[field_name]
        }
        if not changed_fields and not request.snapshot_path:
            raise format_error(
                "revision request makes no change",
                task_id=current.case_id,
                field="revision",
            )
        needs_replacement = bool(changed_fields & {"adapter_dim", "num_layers", "base_model_rev", "adapter_type"})
        if updated.revision > current.revision and not request.snapshot_path:
            raise format_error(
                "a revision change requires a replacement snapshot",
                task_id=current.case_id,
                field="snapshot_path",
            )
        if needs_replacement and not request.snapshot_path:
            raise format_error(
                "adapter structure changes require a replacement snapshot",
                task_id=current.case_id,
                field="snapshot_path",
            )
        return updated

    def _prepare_snapshot_pair(
        self,
        *,
        updated: NormalizedTask,
        source_path: str,
        catalog_root: Path,
    ) -> tuple[Any, str, str]:
        source = Path(source_path)
        if not source.is_absolute():
            source = (Path.cwd() / source).resolve()
        source_format = detect_format(source)
        snapshot = load_snapshot(source, updated, limits=self.limits, degradation=self.degradation)
        target_root = catalog_root / "revisions"
        target_root.mkdir(parents=True, exist_ok=True)
        v1_path = target_root / f"{updated.case_id}.r{updated.revision}.v1.json"
        v2_path = target_root / f"{updated.case_id}.r{updated.revision}.v2"
        write_v1(snapshot, v1_path, provenance={"source": "revision", "source_path": str(source)})
        write_v2(
            snapshot,
            v2_path,
            provenance={"source": "revision", "source_path": str(source)},
            generation=snapshot.generation,
        )
        reloaded_v1 = load_snapshot(v1_path, updated, limits=self.limits, degradation=self.degradation)
        reloaded_v2 = load_snapshot(v2_path, updated, limits=self.limits, degradation=self.degradation)
        if not (
            reloaded_v1.semantic_hash
            == reloaded_v2.semantic_hash
            == snapshot.semantic_hash
        ):
            raise stale_result(
                "revision snapshot pair failed semantic equivalence",
                task_id=updated.case_id,
                field="snapshot_version.semantic_hash",
                details={
                    "source_format": source_format,
                    "v1": reloaded_v1.semantic_hash,
                    "v2": reloaded_v2.semantic_hash,
                },
            )
        return snapshot, str(v1_path.resolve()), str(v2_path.resolve())

    def apply(self, requests: list[RevisionRequest]) -> dict[str, Any]:
        if not requests:
            raise format_error("at least one revision request is required", field="requests")
        with RevisionLock(self.state_dir):
            tasks = parse_tasks(self.tasks_path)
            catalog = Catalog(self.catalog_path)
            by_id = {task.case_id: task for task in tasks}
            updated_tasks: dict[str, NormalizedTask] = {}
            snapshot_data: dict[str, dict[str, Any]] = {}
            impacted: dict[str, dict[str, Any]] = {}
            for request in requests:
                if request.case_id not in by_id:
                    raise format_error(
                        "revision request references unknown task",
                        task_id=request.case_id,
                        field="case_id",
                    )
                if request.case_id in updated_tasks:
                    raise format_error(
                        "task appears more than once in one revision transaction",
                        task_id=request.case_id,
                        field="case_id",
                    )
                current = by_id[request.case_id]
                updated = self._build_updated_task(current, request)
                validate_revision_transition(current, updated)
                old_path = catalog.snapshot_path(current.case_id, "v2")
                old_v1_path = catalog.snapshot_path(current.case_id, "v1")
                old_snapshot = load_snapshot(old_path, current, limits=self.limits, degradation=self.degradation)
                if request.snapshot_path:
                    new_snapshot, v1_path, v2_path = self._prepare_snapshot_pair(
                        updated=updated,
                        source_path=request.snapshot_path,
                        catalog_root=catalog.root,
                    )
                    snapshots_to_store = True
                else:
                    new_snapshot = load_snapshot(
                        old_path,
                        updated,
                        limits=self.limits,
                        degradation=self.degradation,
                    )
                    v1_path, v2_path = old_v1_path, old_path
                    snapshots_to_store = False
                changed_fields = {
                    field_name
                    for field_name in current.canonical_fields()
                    if current.canonical_fields()[field_name] != updated.canonical_fields()[field_name]
                }
                snapshot_replaced = old_snapshot.semantic_hash != new_snapshot.semantic_hash
                if snapshot_replaced:
                    changed_fields.add("snapshot_path")
                stages = _impacted_stages(
                    changed_fields,
                    lr_changed="lr" in changed_fields,
                    steps_changed=bool(changed_fields & {"steps", "micro_batch"}),
                )
                if snapshot_replaced:
                    stages = ["INITIALIZE", "GRAD_ACCUM", "OPTIMIZER_UPDATE", "EXPORT"]
                keys_before = _cache_keys_for(current, old_snapshot.semantic_hash)
                keys_after = _cache_keys_for(updated, new_snapshot.semantic_hash)
                impacted[current.case_id] = {
                    "task_id": current.case_id,
                    "changed_fields": sorted(changed_fields),
                    "affected_stages": stages,
                    "snapshot_replaced": snapshot_replaced,
                    "cache_keys_before": keys_before,
                    "cache_keys_after": keys_after,
                    "cache_keys_changed": {
                        key: keys_before[key] != keys_after[key]
                        for key in sorted(keys_before)
                    },
                }
                updated_tasks[current.case_id] = updated
                if snapshots_to_store:
                    snapshot_data[current.case_id] = {
                        "snapshot": new_snapshot,
                        "v1": v1_path,
                        "v2": v2_path,
                    }
            all_updated = [updated_tasks.get(task.case_id, task) for task in tasks]
            affected_count = len(impacted)
            if affected_count > self.limits.max_recompute_tasks:
                self.degradation.record(
                    reason="recompute_task_limit",
                    priority=3,
                    action="batch_recompute_in_case_id_order",
                    details={
                        "affected_tasks": affected_count,
                        "limit": self.limits.max_recompute_tasks,
                    },
                )
            generation = catalog.increment_generation()
            new_tasks_checksum = write_tasks(self.tasks_path, all_updated)
            for task_id, prepared in snapshot_data.items():
                snapshot = prepared["snapshot"]
                catalog.replace_snapshot(
                    task_id,
                    rev=snapshot.revision,
                    snapshot_id=snapshot.snapshot_id,
                    semantic_hash=snapshot.semantic_hash,
                    format_version="v1",
                    absolute_path=prepared["v1"],
                )
                catalog.replace_snapshot(
                    task_id,
                    rev=snapshot.revision,
                    snapshot_id=snapshot.snapshot_id,
                    semantic_hash=snapshot.semantic_hash,
                    format_version="v2",
                    absolute_path=prepared["v2"],
                )
            catalog_checksum = catalog.save()
            report = {
                "ok": True,
                "operation": "revision_commit",
                "config_generation": generation,
                "affected_task_count": affected_count,
                "unaffected_task_count": len(tasks) - affected_count,
                "affected_tasks": sorted(impacted),
                "unaffected_tasks": sorted(set(by_id) - set(impacted)),
                "impact": impacted,
                "assertion": (
                    "content-addressed cache keys preserve unaffected prefixes; "
                    "only these task suffixes require recomputation"
                ),
                "degradation": self.degradation.as_list(),
                "tasks_checksum": new_tasks_checksum,
                "catalog_checksum": catalog_checksum,
                "revision_requests": [request.as_dict() for request in requests],
            }
            reports = self.state_dir / "reports"
            reports.mkdir(parents=True, exist_ok=True)
            path = reports / f"revision-g{generation}.json"
            atomic_write_bytes(path, canonical_bytes(report))
            history_path = self.state_dir / "revision-history.json"
            history: list[dict[str, Any]] = []
            if history_path.is_file():
                try:
                    loaded_history = json.loads(history_path.read_text(encoding="utf-8"))
                    if isinstance(loaded_history, list):
                        history = loaded_history
                except Exception:
                    history = []
            history.append(
                {
                    "config_generation": generation,
                    "affected_tasks": sorted(impacted),
                    "impact": impacted,
                    "degradation": self.degradation.as_list(),
                }
            )
            atomic_write_bytes(history_path, canonical_bytes(history))
            report["report_path"] = str(path)
            return report


__all__ = ["RevisionLock", "RevisionRequest", "RevisionService"]

