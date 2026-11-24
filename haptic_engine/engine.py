from __future__ import annotations

import json
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .budget import BudgetCancelled, InflightBudget
from .errors import HapticError
from .models import (
    COMMANDS_SCHEMA,
    ENGINE_SCHEMA,
    Bundle,
    parse_bundle,
)
from .packing import BlockPlan, DevicePlan, plan_device, validate_plan
from .state import StateStore, state_relative_path
from .sync import diagnose_anchors

UTC = timezone.utc


@dataclass
class RunResult:
    status: str
    exit_code: int
    report: dict[str, Any] | None = None
    commands_path: Path | None = None
    report_path: Path | None = None
    interrupt_path: Path | None = None


@dataclass
class _WorkerResult:
    plan: DevicePlan | None
    interrupted: bool
    reused_blocks: int = 0
    computed_blocks: int = 0
    missing_blocks: int = 0
    completed_blocks: list[str] = field(default_factory=list)
    digest_issues: list[dict[str, Any]] = field(default_factory=list)
    removed_orphans: list[str] = field(default_factory=list)


def _load_json(path: Path, label: str) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except OSError as error:
        raise HapticError(
            "E_IO",
            f"cannot read {label}",
            {"path": str(path), "message": str(error)},
        ) from error
    except (ValueError, TypeError) as error:
        raise HapticError(
            "E_JSON",
            f"invalid JSON in {label}",
            {"path": str(path), "message": str(error)},
        ) from error


def load_bundle_from_workspace(
    workspace: Path | str,
    devices_path: Path | None = None,
    events_path: Path | None = None,
    config_path: Path | None = None,
) -> Bundle:
    workspace = Path(workspace).resolve()
    devices_path = devices_path or workspace / "devices.json"
    events_path = events_path or workspace / "events.json"
    config_path = config_path or workspace / "config.json"
    devices_doc = _load_json(devices_path, "device manifest")
    events_doc = _load_json(events_path, "event script")
    config_doc = _load_json(config_path, "engine config")
    return parse_bundle(workspace, devices_doc, events_doc, config_doc)


class HapticEngine:
    def __init__(
        self,
        bundle: Bundle,
        on_block: Callable[[str, int], None] | None = None,
    ) -> None:
        self.bundle = bundle
        self.on_block = on_block

    def _signal_exit_code(self, signal_info: dict[str, Any] | None) -> int:
        signal_number = None
        if signal_info:
            signal_number = signal_info.get("signal")
        if signal_number == 15:
            return 143
        return 130

    def _interrupt(
        self,
        store: StateStore,
        signal_info: dict[str, Any] | None,
        completed_blocks: list[str],
        digest_issues: list[dict[str, Any]],
    ) -> RunResult:
        exit_code = self._signal_exit_code(signal_info)
        payload = {
            "schema_version": "haptic-interrupt-v1",
            "version_id": self.bundle.version_id,
            "state": "interrupted_at_block_boundary",
            "signal": signal_info.get("signal") if signal_info else None,
            "exit_code": exit_code,
            "completed_blocks": completed_blocks,
            "digest_issues": digest_issues,
            "created_at_utc": datetime.now(UTC).isoformat(),
        }
        path = store.write_interrupt(payload)
        return RunResult(
            status="interrupted",
            exit_code=exit_code,
            interrupt_path=path,
        )

    def _schedule_block(
        self,
        store: StateStore,
        budget: InflightBudget,
        block: BlockPlan,
        cancelled: Callable[[], bool] | None = None,
    ) -> None:
        budget.acquire(
            block.device_id,
            block.block_index,
            block.command_count,
            cancelled=cancelled,
        )
        try:
            store.write_block(block.device_id, block.filename, block.payload)
        finally:
            budget.release(block.command_count)

    def _process_device(
        self,
        store: StateStore,
        budget: InflightBudget,
        device_id: str,
        events: tuple,
        stop_event: threading.Event,
    ) -> _WorkerResult:
        if stop_event.is_set():
            return _WorkerResult(plan=None, interrupted=True)
        plan = plan_device(self.bundle, self.bundle.device_by_id[device_id], events)
        try:
            validate_plan(plan)
        except AssertionError as error:
            raise HapticError(
                "E_INTERNAL",
                "generated plan failed deterministic invariants",
                {"device_id": device_id, "message": str(error)},
            ) from error
        result = _WorkerResult(plan=plan, interrupted=False)
        try:
            for block in plan.blocks:
                if stop_event.is_set():
                    result.interrupted = True
                    break
                path = store.block_path(device_id, block.filename)
                inspection = store.inspect_block(path, block.payload)
                if inspection["ok"]:
                    result.reused_blocks += 1
                else:
                    if inspection["reason"] == "missing":
                        result.missing_blocks += 1
                    else:
                        result.digest_issues.append(inspection)
                    try:
                        self._schedule_block(
                            store,
                            budget,
                            block,
                            cancelled=stop_event.is_set,
                        )
                    except BudgetCancelled:
                        result.interrupted = True
                        break
                    result.computed_blocks += 1
                result.completed_blocks.append(f"{device_id}:{block.filename}")
                if self.on_block is not None:
                    self.on_block(device_id, block.block_index)
                if stop_event.is_set():
                    result.interrupted = True
                    break
        finally:
            expected = {block.filename for block in plan.blocks}
            result.removed_orphans = store.remove_orphan_blocks(device_id, expected)
        return result

    def _audit_blocks(
        self,
        store: StateStore,
        budget: InflightBudget,
        plans: list[DevicePlan],
        stop_event: threading.Event,
    ) -> tuple[list[dict[str, Any]], int]:
        issues: list[dict[str, Any]] = []
        repaired = 0
        for plan in plans:
            for block in plan.blocks:
                if stop_event.is_set():
                    return issues, repaired
                path = store.block_path(plan.device.id, block.filename)
                inspection = store.inspect_block(path, block.payload)
                if inspection["ok"]:
                    continue
                issues.append(inspection)
                self._schedule_block(store, budget, block)
                repaired += 1
                recheck = store.inspect_block(path, block.payload)
                if not recheck["ok"]:
                    raise HapticError(
                        "E_DIGEST",
                        "frame block could not be repaired from deterministic input",
                        {
                            "device_id": plan.device.id,
                            "block_index": block.block_index,
                            "inspection": recheck,
                        },
                    )
        return issues, repaired

    def _read_streams(
        self,
        store: StateStore,
        plans: list[DevicePlan],
    ) -> tuple[dict[str, tuple[dict[str, Any], ...]], int]:
        streams: dict[str, tuple[dict[str, Any], ...]] = {}
        verified = 0
        for plan in plans:
            commands: list[dict[str, Any]] = []
            for block in plan.blocks:
                path = store.block_path(plan.device.id, block.filename)
                data = store.read_json(path)
                inspection = store.inspect_block(path, block.payload)
                if not inspection["ok"]:
                    raise HapticError(
                        "E_DIGEST",
                        "frame block failed final digest verification",
                        {
                            "device_id": plan.device.id,
                            "block_index": block.block_index,
                            "inspection": inspection,
                        },
                    )
                commands.extend(data["commands"])
                verified += 1
            if tuple(commands) != plan.commands:
                raise HapticError(
                    "E_DIGEST",
                    "persisted command stream differs from deterministic plan",
                    {"device_id": plan.device.id},
                )
            streams[plan.device.id] = tuple(commands)
        return streams, verified

    def run(
        self,
        stop_event: threading.Event | None = None,
        signal_info: dict[str, Any] | None = None,
    ) -> RunResult:
        stop_event = stop_event or threading.Event()
        config = self.bundle.config
        store = StateStore(
            self.bundle.workspace, config.state_dir, config.export_dir
        )
        prepare_info = store.prepare(
            self.bundle.version_id,
            self.bundle.manifest_digest,
            self.bundle.events_digest,
        )
        budget = InflightBudget(config.inflight_command_limit)
        events_by_device = self.bundle.events_by_device
        worker_results: dict[str, _WorkerResult] = {}
        fatal_errors: list[BaseException] = []
        futures: dict[Future[_WorkerResult], str] = {}
        with ThreadPoolExecutor(
            max_workers=config.global_concurrency,
            thread_name_prefix="haptic-synth",
        ) as executor:
            for device in self.bundle.devices:
                future = executor.submit(
                    self._process_device,
                    store,
                    budget,
                    device.id,
                    events_by_device[device.id],
                    stop_event,
                )
                futures[future] = device.id
            for future in as_completed(futures):
                device_id = futures[future]
                try:
                    worker_results[device_id] = future.result()
                except Exception as error:
                    fatal_errors.append(error)
                    stop_event.set()
        if fatal_errors:
            first = fatal_errors[0]
            if isinstance(first, HapticError):
                raise first
            raise HapticError(
                "E_IO",
                "device synthesis worker failed",
                {"message": str(first), "type": type(first).__name__},
            ) from first

        completed_blocks = [
            item
            for device_id in sorted(worker_results)
            for item in worker_results[device_id].completed_blocks
        ]
        interrupt_worker = any(result.interrupted for result in worker_results.values())
        if stop_event.is_set() or interrupt_worker:
            digest_issues = [
                issue
                for result in worker_results.values()
                for issue in result.digest_issues
            ]
            return self._interrupt(
                store, signal_info, completed_blocks, digest_issues
            )

        plans = [
            worker_results[device.id].plan
            for device in self.bundle.devices
            if worker_results[device.id].plan is not None
        ]
        plans.sort(key=lambda plan: plan.device.id)
        audit_issues, repaired_blocks = self._audit_blocks(
            store, budget, plans, stop_event
        )
        if stop_event.is_set():
            digest_issues = [
                issue
                for result in worker_results.values()
                for issue in result.digest_issues
            ] + audit_issues
            return self._interrupt(
                store, signal_info, completed_blocks, digest_issues
            )

        streams, verified_blocks = self._read_streams(store, plans)
        anchor_diagnostics = diagnose_anchors(self.bundle, streams)

        if stop_event.is_set():
            digest_issues = [
                issue
                for result in worker_results.values()
                for issue in result.digest_issues
            ] + audit_issues
            return self._interrupt(
                store, signal_info, completed_blocks, digest_issues
            )

        business_codes: list[str] = []
        all_digest_issues = [
            issue
            for result in worker_results.values()
            for issue in result.digest_issues
        ] + audit_issues
        missing_recomputed = sum(
            result.missing_blocks for result in worker_results.values()
        )
        if all_digest_issues or (
            prepare_info["reused_state"] and missing_recomputed > 0
        ):
            business_codes.append("E_DIGEST")

        devices_report: list[dict[str, Any]] = []
        commands_payload_devices: dict[str, Any] = {}
        total_commands = 0
        total_splits = 0
        total_blocks = 0
        for plan in plans:
            device = plan.device
            total_commands += len(plan.commands)
            total_splits += plan.split_count
            total_blocks += len(plan.blocks)
            reasons: dict[str, int] = {}
            if plan.split_count:
                reasons["FRAME_COMMAND_LIMIT"] = plan.split_count
            devices_report.append(
                {
                    "device_id": device.id,
                    "sample_rate_hz": device.sample_rate_hz,
                    "frame_duration_ms": device.frame_duration_ms,
                    "quantization_levels": device.quantization_levels,
                    "max_commands_per_frame": device.max_commands_per_frame,
                    "sample_count": len(plan.levels),
                    "frame_block_count": len(plan.blocks),
                    "command_count": len(plan.commands),
                    "split_count": plan.split_count,
                    "split_reasons": reasons,
                }
            )
            commands_payload_devices[device.id] = {
                "sample_rate_hz": device.sample_rate_hz,
                "frame_duration_ms": device.frame_duration_ms,
                "strength_min": device.strength_min,
                "strength_max": device.strength_max,
                "quantization_levels": device.quantization_levels,
                "max_commands_per_frame": device.max_commands_per_frame,
                "sample_count": len(plan.levels),
                "frame_block_count": len(plan.blocks),
                "command_count": len(plan.commands),
                "split_count": plan.split_count,
                "commands": list(plan.commands),
            }

        commands_payload = {
            "schema_version": COMMANDS_SCHEMA,
            "version_id": self.bundle.version_id,
            "device_order": [device.id for device in self.bundle.devices],
            "devices": commands_payload_devices,
        }
        scheduling = budget.snapshot()
        scheduling["executor_workers"] = config.global_concurrency
        report = {
            "schema_version": "haptic-report-v1",
            "engine_schema": ENGINE_SCHEMA,
            "status": "completed",
            "ok": True,
            "version_id": self.bundle.version_id,
            "manifest_digest": self.bundle.manifest_digest,
            "events_digest": self.bundle.events_digest,
            "generated_at_utc": datetime.now(UTC).isoformat(),
            "workspace": str(self.bundle.workspace),
            "config": config.to_dict(),
            "state": {
                **prepare_info,
                "state_dir": state_relative_path(store, store.state_dir),
                "export_dir": state_relative_path(store, store.export_dir),
                "reused_blocks": sum(
                    result.reused_blocks for result in worker_results.values()
                ),
                "computed_blocks": sum(
                    result.computed_blocks for result in worker_results.values()
                ),
                "removed_orphans": [
                    f"{device_id}:{filename}"
                    for device_id in sorted(worker_results)
                    for filename in worker_results[device_id].removed_orphans
                ],
            },
            "totals": {
                "devices": len(plans),
                "frame_blocks": total_blocks,
                "commands": total_commands,
                "splits": total_splits,
            },
            "devices": devices_report,
            "anchor_diagnostics": anchor_diagnostics,
            "business_codes": business_codes,
            "digest": {
                "algorithm": "sha256",
                "verified_blocks": verified_blocks,
                "repaired_blocks": repaired_blocks,
                "worker_repaired_blocks": sum(
                    len(result.digest_issues)
                    for result in worker_results.values()
                ),
                "missing_recomputed": missing_recomputed,
                "issues": all_digest_issues,
            },
            "scheduling": scheduling,
        }
        try:
            commands_path, report_path = store.write_exports(
                commands_payload, report
            )
        except OSError as error:
            raise HapticError(
                "E_IO",
                "failed to atomically export command stream and report",
                {"message": str(error)},
            ) from error
        store.clear_interrupt()
        return RunResult(
            status="completed",
            exit_code=0,
            report=report,
            commands_path=commands_path,
            report_path=report_path,
        )
