from __future__ import annotations

import copy
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import numpy as np

from .blocks import BlockCache, stitch_blocks, verify_stitch
from .clock import LogicalClock
from .config import config_checksum, load_config
from .errors import AppError, ErrorCode, ErrorLedger, error_response
from .hashing import array_digest
from .io_utils import read_jsonl
from .pipeline import GenerationContext, build_context, run_global
from .planner import estimate_latency, plan_blocks
from .snapshots import SnapshotStore


class DiagnosticService:
    def __init__(self, workspace: str | Path = "artifacts/runtime",
                 config: dict[str, Any] | None = None,
                 config_path: str | Path | None = None,
                 tasks: list[dict[str, Any]] | None = None,
                 tasks_path: str | Path | None = None,
                 error_ledger: ErrorLedger | None = None,
                 clock: LogicalClock | None = None) -> None:
        self.workspace = Path(workspace)
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.config = config or load_config(config_path)
        self.clock = clock or LogicalClock()
        self.errors = error_ledger or ErrorLedger(self.workspace / "errors.jsonl")
        self.snapshots = SnapshotStore(self.workspace / "snapshots", self.clock, self.errors)
        self.cache = BlockCache()

        if tasks is not None:
            self.tasks = {str(task["task_id"]): task for task in tasks}
        elif tasks_path is not None:
            self.tasks = {str(task["task_id"]): task for task in read_jsonl(tasks_path)}
        else:
            self.tasks = {}

        self._contexts: dict[str, GenerationContext] = {}
        self._global_results: dict[str, dict[str, Any]] = {}
        self._references: dict[str, np.ndarray] = {}
        self._plans: dict[str, dict[str, Any]] = {}
        self.active_plan: dict[str, Any] | None = None
        self.active_task_id: str | None = None
        self._ready_items: dict[int, dict[str, Any]] = {}
        self.first_block_observation: dict[str, Any] | None = None

        self._stage_state_lock = threading.Lock()
        self._submission_in_progress = False
        self._ready_lock = threading.RLock()
        self._parallel_limit = int(self.config["resource_limits"].get("max_parallel_tasks", 4))
        self._generation_slots = threading.BoundedSemaphore(max(1, self._parallel_limit))

    def _fail(self, error: AppError) -> AppError:
        self.errors.record(error)
        return error

    def get_task(self, task_id: str) -> dict[str, Any]:
        if task_id not in self.tasks:
            error = AppError(ErrorCode.TASK_NOT_FOUND, f"unknown task_id: {task_id}",
                             {"task_id": task_id, "known": sorted(self.tasks)})
            raise self._fail(error)
        return self.tasks[task_id]

    def set_reference(self, task_id: str, reference: np.ndarray | str | Path) -> None:
        if isinstance(reference, (str, Path)):
            from .io_utils import load_reference
            reference = load_reference(reference)
        self._references[task_id] = np.asarray(reference)

    def _context_key(self, task_id: str, anchor_strength: float | None) -> str:
        reference = self._references.get(task_id)
        reference_hash = array_digest(reference) if reference is not None else "none"
        strength = "default" if anchor_strength is None else str(float(anchor_strength))
        return f"{task_id}:{config_checksum(self.config)[:12]}:{strength}:{reference_hash[:12]}"

    def get_context(self, task_id: str, anchor_strength: float | None = None) -> GenerationContext:
        key = self._context_key(task_id, anchor_strength)
        if key not in self._contexts:
            task = self.get_task(task_id)
            reference = self._references.get(task_id)
            self._contexts[key] = build_context(task, self.config, reference, anchor_strength)
        return self._contexts[key]

    @contextmanager
    def submission_lease(self):
        with self._stage_state_lock:
            if self._submission_in_progress:
                    raise AppError(
                        ErrorCode.STAGE_BUSY,
                        "another block plan submission is already active",
                        {"stage": "plan_submission"},
                    )
            self._submission_in_progress = True
        try:
            yield {"stage": "plan_submission", "acquired_logical_ms": self.clock.now_ms()}
        finally:
            with self._stage_state_lock:
                self._submission_in_progress = False

    def acquire_generation_slot(self, blocking: bool = False) -> bool:
        limit = int(self.config["resource_limits"].get("max_parallel_tasks", 4))
        if limit != self._parallel_limit:
            self._parallel_limit = max(1, limit)
            self._generation_slots = threading.BoundedSemaphore(self._parallel_limit)
        return self._generation_slots.acquire(blocking=blocking)

    def release_generation_slot(self) -> None:
        self._generation_slots.release()

    def submit_plan(self, task_id: str, plan: dict[str, Any] | None = None,
                    expected_plan_version: int | None = None) -> dict[str, Any]:
        task = self.get_task(task_id)
        if expected_plan_version is None:
            current_plan = self.snapshots.load("plan")
            expected_plan_version = int(current_plan["version"]) if current_plan else 0

        try:
            lease = self.submission_lease()
            lease.__enter__()
        except AppError as error:
            raise self._fail(error)

        try:
            current_plan = self.snapshots.load("plan")
            current_version = int(current_plan["version"]) if current_plan else 0
            if expected_plan_version != current_version:
                error = AppError(
                    ErrorCode.STAGE_BUSY,
                    "plan submission conflicted with a newer committed version",
                    {"expected_plan_version": expected_plan_version,
                     "current_plan_version": current_version},
                )
                raise self._fail(error)

            selected_plan = plan or plan_blocks(task, self.config)
            if selected_plan["task_id"] != task_id:
                raise AppError(ErrorCode.INVALID_REQUEST, "plan task_id does not match request")

            context = self.get_context(task_id)
            fusion_payload = {
                "config_checksum": config_checksum(self.config),
                "fusion": context.fusion_resolved,
                "condition_record": context.condition_record,
                "anchor": context.anchor_info,
            }
            fusion_envelope = self.snapshots.commit("fusion", fusion_payload, source=f"submit:{task_id}")
            plan_envelope = self.snapshots.commit("plan", selected_plan, source=f"submit:{task_id}")
            with self._ready_lock:
                self._ready_items = {}
                ready_payload = {
                    "task_id": task_id,
                    "plan_version": plan_envelope["version"],
                    "grid": selected_plan["grid"],
                    "ready_count": 0,
                    "blocks": [],
                }
                ready_envelope = self.snapshots.commit("ready_blocks", ready_payload, source="submit:reset")

            self._plans[task_id] = selected_plan
            self.active_plan = selected_plan
            self.active_task_id = task_id
            self.first_block_observation = None
            return {
                "ok": True,
                "task_id": task_id,
                "plan": selected_plan,
                "fusion_snapshot": self._envelope_metadata(fusion_envelope),
                "plan_snapshot": self._envelope_metadata(plan_envelope),
                "ready_snapshot": self._envelope_metadata(ready_envelope),
            }
        finally:
            lease.__exit__(None, None, None)

    @staticmethod
    def _envelope_metadata(envelope: dict[str, Any]) -> dict[str, Any]:
        return {
            "kind": envelope["kind"],
            "version": envelope["version"],
            "checksum": envelope["checksum"],
            "logical_ms": envelope["logical_ms"],
        }

    def _require_active_plan(self, task_id: str) -> dict[str, Any]:
        if self.active_plan is None or self.active_task_id != task_id:
            if task_id in self._plans:
                self.active_plan = self._plans[task_id]
                self.active_task_id = task_id
            else:
                submit = self.submit_plan(task_id)
                return submit["plan"]
        if self.active_plan is None:
            error = AppError(ErrorCode.PLAN_NOT_READY, "no active block plan")
            raise self._fail(error)
        return self.active_plan

    def _descriptor(self, plan: dict[str, Any], block: int | str | None) -> dict[str, Any]:
        if block is None or block == "center":
            index = int(plan["center_block_index"])
        else:
            index = int(block)
        for descriptor in plan["blocks"]:
            if int(descriptor["block_index"]) == index:
                return descriptor
        error = AppError(ErrorCode.INVALID_REQUEST, "block_index not found in plan",
                         {"block_index": index, "valid": [b["block_index"] for b in plan["blocks"]]})
        raise self._fail(error)

    def completed_block_indexes(self) -> list[int]:
        with self._ready_lock:
            return sorted(int(index) for index in self._ready_items)

    def list_ready_blocks(self) -> dict[str, Any]:
        with self._ready_lock:
            return {
                "task_id": self.active_task_id,
                "ready_count": len(self._ready_items),
                "blocks": [self._ready_items[index] for index in sorted(self._ready_items)],
            }

    def get_block_product(self, task_id: str, block_index: int) -> dict[str, Any] | None:
        plan = self._require_active_plan(task_id)
        descriptor = self._descriptor(plan, block_index)
        context = self.get_context(task_id)
        cache_key = self._cache_key(self.get_task(task_id), descriptor, context)
        record = self.cache.get(cache_key)
        return None if record is None else record["product"]

    def preview_block(self, task_id: str, block: int | str | None = None,
                      anchor_strength: float | None = None,
                      enforce_budget: bool | None = None) -> dict[str, Any]:
        task = self.get_task(task_id)
        plan = self._require_active_plan(task_id)
        descriptor = self._descriptor(plan, block)
        block_index = int(descriptor["block_index"])
        context = self.get_context(task_id, anchor_strength)
        cache_key = self._cache_key(task, descriptor, context)
        cached = self.cache.get(cache_key)
        if cached is not None:
            return {
                "ok": True,
                "cache_hit": True,
                "task_id": task_id,
                "block_index": block_index,
                "bounds": descriptor["pixel_interval"],
                "cache_key": cache_key,
                "product_hash": cached["product_hash"],
                "timing": {"observed_generation_ms": 0.0, "cache_hit": True},
                "ready_blocks": self.completed_block_indexes(),
            }

        if not self.acquire_generation_slot(blocking=False):
            error = AppError(
                ErrorCode.RESOURCE_LIMIT,
                "parallel preview generation limit reached",
                {"resource": "max_parallel_tasks",
                 "limit": self._parallel_limit,
                 "completed_blocks": self.completed_block_indexes()},
            )
            raise self._fail(error)

        start_ns = time.perf_counter_ns()
        try:
            product = self._generate_block_with_context(context, descriptor)
            elapsed_ns = time.perf_counter_ns() - start_ns
        finally:
            self.release_generation_slot()

        observed_ms = elapsed_ns / 1_000_000.0
        estimate = estimate_latency(
            descriptor, int(task["image_shape"][2]), int(task["sampler_steps"]), self.config
        )
        budget_ms = float(self.config["latency_budget_ms"])
        timing = {
            "observed_generation_ms": observed_ms,
            "estimated_generation_ms": estimate["estimated_first_block_ms"],
            "latency_budget_ms": budget_ms,
            "within_budget": observed_ms <= budget_ms,
            "elapsed_ns": elapsed_ns,
            "cache_hit": False,
        }
        if enforce_budget is None:
            scope_all = self.config.get("latency_budget_scope", "first_block") == "all_blocks"
            should_enforce_budget = scope_all or block_index == int(plan["center_block_index"])
        else:
            should_enforce_budget = bool(enforce_budget)
        if should_enforce_budget and observed_ms > budget_ms:
            error = AppError(
                ErrorCode.LATENCY_BUDGET,
                "block generation exceeded latency budget",
                {
                    "task_id": task_id,
                    "block_index": block_index,
                    "observed_ms": observed_ms,
                    "estimated_ms": estimate["estimated_first_block_ms"],
                    "budget_ms": budget_ms,
                    "completed_blocks": self.completed_block_indexes(),
                },
            )
            raise self._fail(error)

        metadata = {
            "task_id": task_id,
            "block_index": block_index,
            "bounds": descriptor["pixel_interval"]["half_open_text"],
            "row_start": descriptor["row_start"],
            "row_end": descriptor["row_end"],
            "col_start": descriptor["col_start"],
            "col_end": descriptor["col_end"],
            "observed_ms": observed_ms,
            "estimated_ms": estimate["estimated_first_block_ms"],
        }
        self.cache.put(cache_key, product, metadata)

        with self._ready_lock:
            self._ready_items[block_index] = {
                **metadata,
                "cache_key": cache_key,
                "product_hash": product["image_digest"],
                "image_u8_hash": product["image_u8_digest"],
                "ready_logical_ms": self.clock.next_ms(),
            }
            ready_payload = {
                "task_id": task_id,
                "plan_version": self.snapshots.current_version("plan"),
                "grid": plan["grid"],
                "ready_count": len(self._ready_items),
                "blocks": [self._ready_items[index] for index in sorted(self._ready_items)],
            }
            ready_envelope = self.snapshots.commit(
                "ready_blocks", ready_payload, source=f"preview:{task_id}:{block_index}"
            )

        if block_index == int(plan["center_block_index"]) and self.first_block_observation is None:
            self.first_block_observation = {
                "task_id": task_id,
                "block_index": block_index,
                **timing,
                "pixel_interval": descriptor["pixel_interval"]["half_open_text"],
                "ready_snapshot_version": ready_envelope["version"],
            }

        return {
            "ok": True,
            "cache_hit": False,
            "task_id": task_id,
            "block_index": block_index,
            "is_center": bool(descriptor.get("is_center", block_index == plan["center_block_index"])),
            "bounds": descriptor["pixel_interval"],
            "cache_key": cache_key,
            "product_hash": product["image_digest"],
            "image_u8_hash": product["image_u8_digest"],
            "timing": timing,
            "ready_snapshot": self._envelope_metadata(ready_envelope),
            "ready_count": len(self._ready_items),
            "ready_blocks": self.completed_block_indexes(),
            "product": product,
        }

    def _cache_key(self, task: dict[str, Any], descriptor: dict[str, Any],
                   context: GenerationContext) -> str:
        from .blocks import build_cache_key

        reference = self._references.get(str(task["task_id"]))
        ref_hash = array_digest(reference) if reference is not None else None
        anchor_strength = float(context.anchor_info.get("strength", 0.0))
        return build_cache_key(
            task,
            {"config_checksum": config_checksum(self.config), "resolved_anchor_strength": anchor_strength},
            descriptor,
            ref_hash,
        )

    def _generate_block_with_context(self, context: GenerationContext,
                                     descriptor: dict[str, Any]) -> dict[str, Any]:
        from .pipeline import generate_block_pixels

        return generate_block_pixels(
            context,
            int(descriptor["row_start"]),
            int(descriptor["row_end"]),
            int(descriptor["col_start"]),
            int(descriptor["col_end"]),
        )

    def preview_center(self, task_id: str, **kwargs: Any) -> dict[str, Any]:
        return self.preview_block(task_id, "center", **kwargs)

    def preview_remaining(self, task_id: str, max_workers: int | None = None,
                          **kwargs: Any) -> dict[str, Any]:
        plan = self._require_active_plan(task_id)
        center = int(plan["center_block_index"])
        remaining = [int(block["block_index"]) for block in plan["blocks"] if int(block["block_index"]) != center]
        workers = max(1, min(len(remaining), max_workers or self._parallel_limit))
        successes: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="block-preview") as executor:
            futures = {executor.submit(self.preview_block, task_id, index, **kwargs): index for index in remaining}
            for future in as_completed(futures):
                index = futures[future]
                try:
                    successes.append(future.result())
                except AppError as error:
                    failures.append({"block_index": index, **error.to_record()})
        return {
            "ok": not failures,
            "requested": remaining,
            "success_count": len(successes),
            "failure_count": len(failures),
            "failures": failures,
            "ready_blocks": self.completed_block_indexes(),
            "successes": successes,
        }

    def generate_global(self, task_id: str, anchor_strength: float | None = None) -> dict[str, Any]:
        context = self.get_context(task_id, anchor_strength)
        key = self._context_key(task_id, anchor_strength)
        if key not in self._global_results:
            started = time.perf_counter_ns()
            result = run_global(self.get_task(task_id), self.config, context=context)
            result["observed_global_ms"] = (time.perf_counter_ns() - started) / 1_000_000.0
            self._global_results[key] = result
        return self._global_results[key]

    def stitch_cached(self, task_id: str) -> tuple[np.ndarray, dict[str, Any], dict[int, dict[str, Any]]]:
        plan = self._require_active_plan(task_id)
        products = self.cache.products_by_index(task_id)
        task = self.get_task(task_id)
        context = self.get_context(task_id)
        stitched = stitch_blocks(context.shape, context.dtype, products, plan["blocks"])
        return stitched, plan, products

    def verify_blocks(self, task_id: str, anchor_strength: float | None = None) -> dict[str, Any]:
        plan = self._require_active_plan(task_id)
        global_result = self.generate_global(task_id, anchor_strength)
        products = self.cache.products_by_index(task_id)
        missing = [
            int(block["block_index"]) for block in plan["blocks"]
            if int(block["block_index"]) not in products
        ]
        if missing:
            error = AppError(ErrorCode.PLAN_NOT_READY, "not all blocks are ready",
                             {"missing_blocks": missing, "ready_blocks": self.completed_block_indexes()})
            raise self._fail(error)
        stitched = stitch_blocks(
            tuple(self.get_task(task_id)["image_shape"]),  # type: ignore[arg-type]
            global_result["image"].dtype,
            products,
            plan["blocks"],
        )
        verification = verify_stitch(stitched, global_result["image"], plan["blocks"])
        verification["ready_blocks"] = self.completed_block_indexes()
        verification["global_digest"] = global_result["image_digest"]
        if not verification["ok"]:
            mismatch = verification["mismatch"] or {}
            error = AppError(
                ErrorCode.BLOCK_MISMATCH,
                "block stitch differs from global generation",
                mismatch,
            )
            verification["error"] = error.to_record()
            raise self._fail(error)
        return verification

    def mismatch_diagnostic(self, task_id: str) -> dict[str, Any]:
        """Safely flip one in-memory stitched value and locate the first diff."""
        plan = self._require_active_plan(task_id)
        global_result = self.generate_global(task_id)
        stitched, _, _ = self.stitch_cached(task_id)
        tampered = stitched.copy()
        tampered[0, 0, 0] = tampered[0, 0, 0] + np.dtype(tampered.dtype).type(0.01)
        verification = verify_stitch(tampered, global_result["image"], plan["blocks"])
        if verification["ok"]:
            return {"ok": False, "message": "fault injection did not create a difference"}
        error = AppError(ErrorCode.BLOCK_MISMATCH, "injected block-boundary mismatch diagnostic",
                         verification["mismatch"] or {})
        recorded = self._fail(error)
        return {"ok": False, **recorded.to_record(), "verification": verification}

    def reload_snapshots_after_restart(self) -> dict[str, Any]:
        previous = {kind: self.snapshots._memory.get(kind) for kind in ("fusion", "plan", "ready_blocks")}
        reconciliation = self.snapshots.reconcile_after_restart(previous)
        plan_envelope = reconciliation["snapshots"].get("plan")
        if plan_envelope:
            self.active_plan = plan_envelope["payload"]
            self.active_task_id = plan_envelope["payload"]["task_id"]
            self._plans[self.active_task_id] = self.active_plan
        ready_envelope = reconciliation["snapshots"].get("ready_blocks")
        if ready_envelope:
            self._ready_items = {
                int(item["block_index"]): item for item in ready_envelope["payload"].get("blocks", [])
            }
        return reconciliation

    def handle(self, action: str, **kwargs: Any) -> dict[str, Any]:
        try:
            method = getattr(self, f"action_{action}", None)
            if method is not None:
                return method(**kwargs)
            if action == "submit":
                return self.submit_plan(**kwargs)
            if action == "center":
                return self.preview_center(**kwargs)
            if action == "remaining":
                return self.preview_remaining(**kwargs)
            if action == "verify":
                return self.verify_blocks(**kwargs)
            if action == "global":
                return {"ok": True, **{k: v for k, v in self.generate_global(**kwargs).items()
                                       if k not in ("context",)}}
            raise AppError(ErrorCode.INVALID_REQUEST, f"unknown action: {action}")
        except AppError as error:
            return error_response(error)
        except Exception as error:  # pragma: no cover - facade safety boundary
            app_error = AppError("E_INTERNAL", str(error), {"type": type(error).__name__})
            return error_response(app_error)
