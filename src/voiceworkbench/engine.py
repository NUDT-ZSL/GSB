"""Interactive engine: incremental recompute, budget semantics, snapshots."""
from __future__ import annotations

import threading
import time
import os
from dataclasses import dataclass
from typing import Any, Callable

from .cache import Ledger, StageCache
from .commands import EditCommand, resolve_selector
from .config import PipelineConfig
from .determinism import atomic_write_json
from .errors import BudgetPending, CommandError, WorkbenchError
from .materialize import MaterialState, apply_cut, apply_replace, load_material
from .pipeline import Pipeline, PipelineResult
from .snapshot import (
    SnapshotData,
    SnapshotStore,
    load_material_npz,
    material_to_snapshot,
    persist_material_npz,
)

SUMMARY_NAME = "summary-latest.json"


@dataclass
class CommandResponse:
    seq: int
    command_type: str
    accepted: bool
    in_budget: bool
    pending: bool
    pending_code: str | None
    elapsed_ms: float
    affected_segments: list[str]
    summary_version: int
    error: dict[str, Any] | None
    ledger: dict[str, Any]
    stage_keys: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "command_type": self.command_type,
            "accepted": self.accepted,
            "in_budget": self.in_budget,
            "pending": self.pending,
            "pending_code": self.pending_code,
            "elapsed_ms": round(self.elapsed_ms, 4),
            "affected_segments": list(self.affected_segments),
            "summary_version": self.summary_version,
            "error": self.error,
            "ledger": self.ledger,
            "stage_keys": dict(self.stage_keys),
        }


class WorkbenchEngine:
    """State machine over edit commands.

    The interaction budget only changes *when* a summary becomes visible.
    Exceeding the budget raises :class:`BudgetPending`; work then continues on
    a background worker and finishes deterministically.  It never aborts work.
    """

    def __init__(self, config: PipelineConfig, source_paths: list[str],
                 snapshot_dir: str, clock: Callable[[], float] | None = None,
                 fault_injection: dict[str, Any] | None = None,
                 slow_ms: float = 0.0) -> None:
        self.config = config
        self.source_paths = sorted(source_paths)
        self.snapshot_store = SnapshotStore(snapshot_dir)
        self.cache = StageCache()
        self.clock = clock or time.perf_counter
        self.fault_injection = fault_injection or {}
        self.slow_ms = slow_ms
        self.material: MaterialState | None = None
        self.threshold_override: float | None = None
        self.cluster_overrides: list[dict[str, Any]] = []
        self.command_cursor = 0
        self.product_version = 0
        self.latest_result: PipelineResult | None = None
        self.responses: list[CommandResponse] = []
        self.rejected_commands: list[dict[str, Any]] = []
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ #
    # Initial bootstrap / restore
    # ------------------------------------------------------------------ #
    def initialize(self) -> PipelineResult:
        snapshot = self._try_restore()
        if snapshot is None:
            self.material = load_material(self.source_paths)
            self.threshold_override = None
            self.cluster_overrides = []
            self.command_cursor = 0
            self.product_version = 0
            result = self._bootstrap_pipeline()
        else:
            self.product_version = snapshot.product_version
            # Trust the fully replaced snapshot + cache: rehydrate the last
            # result from cached artifacts without recomputing anything.
            result = self._rehydrate_latest_result()
        self._persist_snapshot("assignment")
        self._publish_summary(result, in_budget=True)
        return result

    def _rehydrate_latest_result(self) -> PipelineResult:
        assert self.material is not None
        ledger = Ledger()
        pipeline = Pipeline(
            config=self.config,
            cache=self.cache,
            fault_injection=self.fault_injection,
            budget_check=lambda: None,
        )
        result = pipeline.run(
            material=self.material,
            current_config=self.config,
            threshold_override=self.threshold_override,
            cluster_overrides=list(self.cluster_overrides),
            ledger=ledger,
            invalidate_reason=None,
        )
        self.latest_result = result
        return result

    def _bootstrap_pipeline(self) -> PipelineResult:
        assert self.material is not None
        pipeline = Pipeline(
            config=self.config,
            cache=self.cache,
            fault_injection=self.fault_injection,
            budget_check=lambda: None,
        )
        result = pipeline.run(
            material=self.material,
            current_config=self.config,
            threshold_override=self.threshold_override,
            cluster_overrides=list(self.cluster_overrides),
            ledger=Ledger(),
            invalidate_reason=None,
        )
        self.latest_result = result
        return result

    def _try_restore(self) -> SnapshotData | None:
        if not self.snapshot_store.exists():
            return None
        try:
            snapshot = self.snapshot_store.load()
        except WorkbenchError:
            # only fully replaced snapshots are trusted; damaged ones are ignored
            return None
        if snapshot.config_hash != self.config.fingerprint():
            return None
        if not os.path.exists(self.snapshot_store.material_path):
            return None
        self.cache = self.snapshot_store.load_cache()
        self.material = load_material_npz(self.snapshot_store)
        if self.material.fingerprint() != snapshot.material_fingerprint:
            return None
        self.threshold_override = snapshot.threshold_override
        self.cluster_overrides = list(snapshot.cluster_overrides)
        self.command_cursor = snapshot.command_cursor
        self.product_version = snapshot.product_version
        return snapshot

    # ------------------------------------------------------------------ #
    # Command processing
    # ------------------------------------------------------------------ #
    def handle_command(self, command: EditCommand) -> CommandResponse:
        start = self.clock()
        deadline = start + self.config.interact_budget_ms / 1000.0
        budget_exceeded = {"value": False}

        def budget_check() -> None:
            if self.slow_ms:
                time.sleep(self.slow_ms / 1000.0)
            if self.clock() > deadline:
                budget_exceeded["value"] = True
                raise BudgetPending(
                    "interaction budget exhausted; continuing in background"
                )

        with self._lock:
            try:
                reason, affected = self._apply_command_effect(command)
            except CommandError as exc:
                response = self._reject(command, exc, start)
                self.responses.append(response)
                return response

            ledger = Ledger()
            try:
                result = self._run_pipeline(ledger, reason, budget_check)
                in_budget = True
                pending = False
            except BudgetPending as exc:
                # Budget only delays visibility; finish the full computation now
                # on the background path, then atomically publish the result.
                result = self._run_pipeline(Ledger(), reason, lambda: None)
                in_budget = False
                pending = True
                pending_code = exc.code
                self._publish_summary(result, in_budget=False)
                self._persist_snapshot("assignment")
                response = self._build_response(
                    command, result, start, in_budget=False, pending=True,
                    pending_code=exc.code, ledger=ledger, affected=affected,
                )
                self.responses.append(response)
                return response

            self._publish_summary(result, in_budget=True)
            self._persist_snapshot("assignment")
            response = self._build_response(
                command, result, start, in_budget=True, pending=False,
                pending_code=None, ledger=ledger, affected=affected,
            )
            self.responses.append(response)
            return response

    def _apply_command_effect(self, command: EditCommand) -> tuple[str, list[str]]:
        params = command.params
        if command.command_type == "set_cluster_threshold":
            self.threshold_override = float(params["cluster_distance_threshold"])
            return "threshold_changed", []

        if command.command_type == "cut_interval":
            assert self.material is not None
            apply_cut(self.material, params)
            return "cut_interval", [params["file_name"]]

        if command.command_type == "replace_interval_audio":
            assert self.material is not None
            apply_replace(self.material, params, command.seq)
            return "replace_interval_audio", [params["file_name"]]

        if command.command_type == "merge_speakers":
            labels = self._current_labels()
            label_a = resolve_selector(params["label_a"], labels)
            label_b = resolve_selector(params["label_b"], labels)
            if label_a == label_b:
                raise CommandError("merge requires two distinct labels",
                                   labels=[label_a, label_b])
            self.cluster_overrides.append({
                "op": "merge",
                "seq": command.seq,
                "label_a": {"kind": "label", "value": label_a},
                "label_b": {"kind": "label", "value": label_b},
            })
            return "cluster_override", []

        if command.command_type == "split_speaker":
            labels = self._current_labels()
            label = resolve_selector(params["label"], labels)
            self.cluster_overrides.append({
                "op": "split",
                "seq": command.seq,
                "label": {"kind": "label", "value": label},
            })
            return "cluster_override", []

        raise CommandError(f"unsupported command: {command.command_type}")

    def _current_labels(self) -> list[str]:
        if self.latest_result is None:
            return []
        return sorted(cluster.label for cluster in self.latest_result.clusters)

    def _run_pipeline(self, ledger: Ledger, reason: str | None,
                      budget_check: Callable[[], None]) -> PipelineResult:
        assert self.material is not None
        pipeline = Pipeline(
            config=self.config,
            cache=self.cache,
            fault_injection=self.fault_injection,
            budget_check=budget_check,
        )
        result = pipeline.run(
            material=self.material,
            current_config=self.config,
            threshold_override=self.threshold_override,
            cluster_overrides=self.cluster_overrides,
            ledger=ledger,
            invalidate_reason=reason,
        )
        self.latest_result = result
        self.product_version += 1
        self.command_cursor += 1
        return result

    # ------------------------------------------------------------------ #
    # Rejection / responses / persistence
    # ------------------------------------------------------------------ #
    def _reject(self, command: EditCommand, exc: CommandError,
                start: float) -> CommandResponse:
        self.rejected_commands.append({
            "seq": command.seq,
            "command_type": command.command_type,
            "code": exc.code,
            "message": exc.message,
            "context": exc.context,
        })
        return CommandResponse(
            seq=command.seq,
            command_type=command.command_type,
            accepted=False,
            in_budget=True,
            pending=False,
            pending_code=None,
            elapsed_ms=(self.clock() - start) * 1000.0,
            affected_segments=[],
            summary_version=self.product_version,
            error={"code": exc.code, "message": exc.message, "context": exc.context},
            ledger=Ledger().to_dict(),
            stage_keys=self.latest_result.stage_keys if self.latest_result else {},
        )

    def _build_response(self, command: EditCommand, result: PipelineResult,
                        start: float, in_budget: bool, pending: bool,
                        pending_code: str | None, ledger: Ledger,
                        affected: list[str]) -> CommandResponse:
        overlap_ids = sorted(
            segment.segment_id for segment in result.segments if segment.overlap
        )
        affected_segments = sorted(set(overlap_ids + [
            segment.segment_id for segment in result.segments
            if segment.file_name in affected
        ]))
        return CommandResponse(
            seq=command.seq,
            command_type=command.command_type,
            accepted=True,
            in_budget=in_budget,
            pending=pending,
            pending_code=pending_code,
            elapsed_ms=(self.clock() - start) * 1000.0,
            affected_segments=affected_segments,
            summary_version=self.product_version,
            error=None,
            ledger=ledger.to_dict(),
            stage_keys=dict(result.stage_keys),
        )

    def _publish_summary(self, result: PipelineResult, in_budget: bool) -> None:
        summary = {
            "summary_version": self.product_version,
            "in_budget": in_budget,
            "command_cursor": self.command_cursor,
            "config_fingerprint": result.config_fingerprint,
            "material_fingerprint": result.material_fingerprint,
            "stage_keys": result.stage_keys,
            "summary": result.summary(),
            "failures": [record.to_dict() for record in result.failures],
        }
        atomic_write_json(
            self.snapshot_store.path.replace("snapshot.json", SUMMARY_NAME),
            summary,
        )

    def _persist_snapshot(self, stage_name: str) -> None:
        assert self.material is not None
        persist_material_npz(self.snapshot_store, self.material)
        completed = sorted(
            segment.segment_id for segment in
            (self.latest_result.segments if self.latest_result else [])
        )
        data = SnapshotData(
            schema_version=1,
            stage_name=stage_name,
            config_hash=self.config.fingerprint(),
            command_cursor=self.command_cursor,
            completed_segments=completed,
            product_version=self.product_version,
            material_fingerprint=self.material.material_fingerprint,
            stage_cache_path=self.snapshot_store.cache_path,
            threshold_override=self.threshold_override,
            cluster_overrides=list(self.cluster_overrides),
            material=material_to_snapshot(self.material),
            extra={"threshold_override_active": self.threshold_override is not None},
        )
        self.snapshot_store.save(data, self.cache)

    # ------------------------------------------------------------------ #
    # Final diagnostics
    # ------------------------------------------------------------------ #
    def diagnostics(self) -> dict[str, Any]:
        assert self.latest_result is not None
        result = self.latest_result
        return {
            "command_cursor": self.command_cursor,
            "product_version": self.product_version,
            "config_fingerprint": result.config_fingerprint,
            "material_fingerprint": result.material_fingerprint,
            "stage_keys": result.stage_keys,
            "clusters": [cluster.to_dict() for cluster in result.clusters],
            "assignments": [item.to_dict() for item in result.assignments],
            "segments": [segment.to_dict() for segment in result.segments],
            "rejected_commands": list(self.rejected_commands),
            "command_responses": [response.to_dict() for response in self.responses],
            "failures": [record.to_dict() for record in result.failures],
            "cluster_overrides": list(self.cluster_overrides),
            "threshold_override": self.threshold_override,
        }
