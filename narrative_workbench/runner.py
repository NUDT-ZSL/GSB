from __future__ import annotations

import os
import signal
import time
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .engine import SimulationKernel
from .errors import (
    BudgetError,
    DigestError,
    InputError,
    Interrupted,
    RuntimeTaskError,
    WorkbenchError,
)
from .jsonio import atomic_write_json
from .models import Bundle
from .persistence import SnapshotStore
from .replay import REPLAY_FORMAT, ensure_replay_ok, verify_replay
from .scheduler import BoundedTaskPool, InterruptController, MemoryBudget
from .storyboard import (
    build_frame_specs,
    plan_frame_blocks,
    render_frame_block,
)


@dataclass(frozen=True)
class RunnerConfig:
    workers: int = 2
    memory_budget_bytes: int = 64 * 1024 * 1024
    frame_block_size: int = 4
    state_buffer_bytes: int = 128 * 1024
    fresh: bool = False
    fault_after_boundary: int | None = None
    fault_signal: str = "sigint"

    def __post_init__(self) -> None:
        if self.workers < 1:
            raise InputError("E_CONFIG", "workers must be at least 1")
        if self.frame_block_size < 1:
            raise InputError("E_CONFIG", "frame_block_size must be at least 1")
        if self.memory_budget_bytes <= 0:
            raise InputError("E_CONFIG", "memory_budget_bytes must be positive")
        if self.state_buffer_bytes <= 0:
            raise InputError("E_CONFIG", "state_buffer_bytes must be positive")
        if self.fault_after_boundary is not None and self.fault_after_boundary < 1:
            raise InputError("E_CONFIG", "fault_after_boundary must be at least 1")
        if self.fault_signal not in {"sigint", "sigterm", "sigkill"}:
            raise InputError(
                "E_CONFIG", "fault_signal must be sigint, sigterm, or sigkill"
            )


ProgressCallback = Callable[[dict[str, Any]], None]


def _safe_component(value: str) -> str:
    return "".join(
        character if character.isalnum() or character in "._-" else "_"
        for character in value
    )[:160]


class WorkbenchRunner:
    def __init__(
        self,
        bundle: Bundle,
        *,
        state_dir: str | Path,
        output_dir: str | Path,
        config: RunnerConfig,
        interrupt: InterruptController | None = None,
        progress: ProgressCallback | None = None,
    ) -> None:
        self.bundle = bundle
        self.state_dir = Path(state_dir)
        self.output_dir = Path(output_dir)
        self.config = config
        self.interrupt = interrupt or InterruptController()
        self.progress = progress
        self.store = SnapshotStore(
            self.state_dir,
            version_id=bundle.version_id,
            run_id=bundle.run_id,
            canvas_width=bundle.storyboard.width,
            canvas_height=bundle.storyboard.height,
        )
        self.run_output = (
            self.output_dir / "runs" / _safe_component(bundle.version_id) / _safe_component(bundle.run_id)
        )
        self.result_path = self.run_output / "result.json"
        self.boundary_count = 0
        self._last_sim_sequence: int | None = None
        self._last_frame_sequence: int | None = None
        self._last_frame_index: int | None = None
        self._last_scene_id: str | None = None
        self._last_boundary = "start"
        self._fault_injected = False

    def _emit(self, event: dict[str, Any]) -> None:
        if self.progress is None:
            return
        try:
            self.progress(event)
        except Exception:
            return

    def _block_bytes(self) -> int:
        return (
            self.bundle.storyboard.width
            * self.bundle.storyboard.height
            * self.config.frame_block_size
            * 2
            + 32768
        )

    def _validate_budget(self) -> None:
        block_bytes = self._block_bytes()
        if self.config.memory_budget_bytes < self.config.state_buffer_bytes:
            raise BudgetError(
                "memory budget cannot hold one event state buffer",
                {
                    "memory_budget_bytes": self.config.memory_budget_bytes,
                    "state_buffer_bytes": self.config.state_buffer_bytes,
                },
            )
        if self.config.memory_budget_bytes < block_bytes:
            raise BudgetError(
                "memory budget cannot hold one full-resolution frame block",
                {
                    "memory_budget_bytes": self.config.memory_budget_bytes,
                    "frame_block_bytes": block_bytes,
                    "frame_block_size": self.config.frame_block_size,
                },
            )

    def _archive_output(self, reason: str) -> None:
        if not self.run_output.exists():
            return
        archive_root = self.output_dir / "archive"
        archive_root.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        destination = archive_root / (
            f"{_safe_component(reason)}-{_safe_component(self.bundle.run_id)}-{stamp}-{os.getpid()}"
        )
        if destination.exists():
            destination = archive_root / f"{destination.name}-{time.time_ns()}"
        import shutil

        shutil.move(str(self.run_output), str(destination))

    def _prepare_run_root(self, recovery: dict[str, Any]) -> dict[str, Any]:
        errors = list(recovery["report"]["errors"])
        if self.config.fresh:
            self._archive_output("fresh-run")
            archived = self.store.archive_current_run(reason="fresh")
            if archived:
                self._emit({"event": "archive", "kind": "fresh-run", **archived})
            recovery = self.store.recover()
            recovery["report"]["archive_events"].append(
                archived or {"reason": "fresh-run-empty"}
            )
            recovery["report"]["errors"].extend(errors)
            return recovery

        if recovery["sim_payload"] is None and errors:
            archived = self.store.archive_current_run(
                reason="invalid-recovery-before-head"
            )
            if archived:
                self._emit(
                    {"event": "archive", "kind": "invalid-recovery", **archived}
                )
            recovery = self.store.recover()
            recovery["report"]["errors"] = errors + recovery["report"]["errors"]
            if archived:
                recovery["report"]["archive_events"].append(archived)
        return recovery

    def _maybe_fault(self, *, boundary: str, frame_index: int | None, scene_id: str | None) -> None:
        if self.interrupt.requested:
            return
        if (
            self.config.fault_after_boundary is not None
            and self.boundary_count >= self.config.fault_after_boundary
        ):
            self._fault_injected = True
            if self.config.fault_signal == "sigkill":
                os.kill(os.getpid(), signal.SIGKILL)
            exit_code = 130 if self.config.fault_signal == "sigint" else 143
            self.interrupt.request(
                "SIGINT" if self.config.fault_signal == "sigint" else "SIGTERM",
                exit_code,
            )
            self._emit(
                {
                    "event": "fault-injected",
                    "boundary": boundary,
                    "frame_index": frame_index,
                    "scene_id": scene_id,
                }
            )

    def _raise_if_interrupted(
        self, boundary: str, *, frame_index: int | None, scene_id: str | None
    ) -> None:
        if not self.interrupt.requested:
            return
        raise Interrupted(
            self.interrupt.signal_name or "SIGINT",
            boundary,
            frame_index=frame_index,
            scene_id=scene_id,
            exit_code=self.interrupt.exit_code or 130,
            test_injected=self._fault_injected,
        )

    def _run_simulation(
        self,
        machine: SimulationKernel,
        pool: BoundedTaskPool,
        memory: MemoryBudget,
        recovery_report: dict[str, Any],
    ) -> SimulationKernel:
        self._emit(
            {
                "event": "simulation-start",
                "ledger_count": len(machine.ledger),
                "complete": machine.complete,
            }
        )
        while not machine.complete:
            self._raise_if_interrupted(
                "event",
                frame_index=(
                    machine.ledger[-1]["frame_index"] if machine.ledger else None
                ),
                scene_id=(
                    machine.ledger[-1]["scene_id"] if machine.ledger else None
                ),
            )
            if not memory.try_reserve(self.config.state_buffer_bytes):
                raise BudgetError(
                    "memory budget blocked an event-block state buffer",
                    {
                        "memory_budget_bytes": self.config.memory_budget_bytes,
                        "required_bytes": self.config.state_buffer_bytes,
                    },
                )
            future: Future[Any] = pool.submit(machine.step)
            try:
                while not future.done():
                    time.sleep(0.01)
                record, complete = future.result()
            except WorkbenchError:
                raise
            except BaseException as exc:
                raise RuntimeTaskError(
                    "event-block task failed",
                    {"scene_id": machine.progress.get("current_scene_id"), "error": repr(exc)},
                ) from exc
            finally:
                memory.release(self.config.state_buffer_bytes)

            payload = machine.to_payload()
            envelope = self.store.save_sim(payload)
            self._last_sim_sequence = int(envelope["sequence"])
            self.boundary_count += 1
            self._last_boundary = "event"
            if record is not None:
                self._last_frame_index = record["frame_index"]
                self._last_scene_id = record["scene_id"]
            else:
                self._last_scene_id = machine.progress.get("current_scene_id")
            self._emit(
                {
                    "event": "simulation-boundary",
                    "snapshot_sequence": self._last_sim_sequence,
                    "ledger_count": len(machine.ledger),
                    "record": record,
                    "complete": complete,
                }
            )
            self._maybe_fault(
                boundary="event",
                frame_index=self._last_frame_index,
                scene_id=self._last_scene_id,
            )
            self._raise_if_interrupted(
                "event",
                frame_index=self._last_frame_index,
                scene_id=self._last_scene_id,
            )

        if machine.complete and not machine.ledger:
            envelope = self.store.save_sim(machine.to_payload())
            self._last_sim_sequence = int(envelope["sequence"])
            self.boundary_count += 1
        self._emit(
            {
                "event": "simulation-complete",
                "ledger_count": len(machine.ledger),
                "summary": machine.summary(),
            }
        )
        return machine

    @staticmethod
    def _block_matches_descriptor(
        payload: dict[str, Any], descriptor: dict[str, Any]
    ) -> bool:
        frames = payload.get("frames", [])
        expected_indices = [frame["frame_index"] for frame in descriptor["frames"]]
        actual_indices = [frame.get("frame_index") for frame in frames]
        return (
            payload.get("scene_id") == descriptor["scene_id"]
            and payload.get("block_index") == descriptor["block_index"]
            and actual_indices == expected_indices
        )

    def _render_missing_frames(
        self,
        machine: SimulationKernel,
        pool: BoundedTaskPool,
        memory: MemoryBudget,
        recovered_blocks: dict[str, dict[str, Any]],
        recovery_report: dict[str, Any],
    ) -> None:
        frames = build_frame_specs(self.bundle, machine.ledger)
        descriptors = plan_frame_blocks(
            self.bundle, frames, self.config.frame_block_size
        )
        descriptor_sequences = {
            descriptor["unit_key"]: index + 1
            for index, descriptor in enumerate(descriptors)
        }
        expected_keys = {descriptor["unit_key"] for descriptor in descriptors}
        unexpected = sorted(set(recovered_blocks) - expected_keys)
        if unexpected:
            raise DigestError(
                "valid frame snapshots do not match the expected frame plan",
                {"unexpected_blocks": unexpected},
            )
        for descriptor in descriptors:
            existing = recovered_blocks.get(descriptor["unit_key"])
            if existing is not None and not self._block_matches_descriptor(
                existing, descriptor
            ):
                raise DigestError(
                    "recovered frame block metadata conflicts with the ledger plan",
                    {
                        "unit_key": descriptor["unit_key"],
                        "scene_id": descriptor["scene_id"],
                        "block_index": descriptor["block_index"],
                    },
                )
            if existing is not None:
                self._emit(
                    {
                        "event": "frame-block-ready",
                        "unit_key": descriptor["unit_key"],
                        "scene_id": descriptor["scene_id"],
                        "block_index": descriptor["block_index"],
                        "frames": existing["frames"],
                        "recovered": True,
                    }
                )
        missing = [
            descriptor
            for descriptor in descriptors
            if descriptor["unit_key"] not in recovered_blocks
        ]
        self._emit(
            {
                "event": "frames-plan",
                "total_blocks": len(descriptors),
                "recovered_blocks": len(recovered_blocks),
                "missing_blocks": len(missing),
                "frame_count": len(frames),
            }
        )
        if not missing:
            self._last_frame_sequence = max(
                descriptor_sequences.values(), default=self._last_frame_sequence
            )
            return

        block_bytes = self._block_bytes()
        next_index = 0
        inflight: dict[Future[Any], tuple[dict[str, Any], int]] = {}
        first_error: BaseException | None = None
        drain_for_interrupt = False
        while next_index < len(missing) or inflight:
            while (
                next_index < len(missing)
                and len(inflight) < self.config.workers
                and first_error is None
                and not drain_for_interrupt
            ):
                descriptor = missing[next_index]
                if not memory.try_reserve(block_bytes):
                    break
                future = pool.submit(render_frame_block, self.bundle, descriptor)
                inflight[future] = (descriptor, block_bytes)
                next_index += 1
            if not inflight:
                if first_error is not None:
                    break
                if drain_for_interrupt:
                    break
                if next_index < len(missing):
                    if not memory.try_reserve(block_bytes):
                        raise BudgetError(
                            "memory budget blocked a full-resolution frame block",
                            {
                                "memory_budget_bytes": self.config.memory_budget_bytes,
                                "required_bytes": block_bytes,
                            },
                        )
                    descriptor = missing[next_index]
                    future = pool.submit(
                        render_frame_block, self.bundle, descriptor
                    )
                    inflight[future] = (descriptor, block_bytes)
                    next_index += 1
                continue
            done, _pending = pool.wait(inflight, timeout=0.05)
            if self.interrupt.requested and not drain_for_interrupt:
                drain_for_interrupt = True
            for future in list(done):
                descriptor, reservation = inflight.pop(future)
                try:
                    payload = future.result()
                except BaseException as exc:
                    if first_error is None:
                        first_error = exc
                    pool.cancel_pending()
                    memory.release(reservation)
                    continue
                try:
                    envelope = self.store.save_frame_block(
                        unit_key=descriptor["unit_key"],
                        payload=payload,
                        scene_id=descriptor["scene_id"],
                        block_index=descriptor["block_index"],
                        start_frame=descriptor["start_frame"],
                        end_frame=descriptor["end_frame"],
                        sequence=descriptor_sequences[descriptor["unit_key"]],
                    )
                    self._last_frame_sequence = int(envelope["sequence"])
                    self._last_frame_index = descriptor["end_frame"]
                    self._last_scene_id = descriptor["scene_id"]
                    self._last_boundary = "frame_block"
                    self.boundary_count += 1
                    self._emit(
                        {
                            "event": "frame-block-ready",
                            "unit_key": descriptor["unit_key"],
                            "scene_id": descriptor["scene_id"],
                            "block_index": descriptor["block_index"],
                            "frames": payload["frames"],
                        }
                    )
                finally:
                    memory.release(reservation)
                if first_error is None:
                    self._maybe_fault(
                        boundary="frame_block",
                        frame_index=descriptor["end_frame"],
                        scene_id=descriptor["scene_id"],
                    )
                    if self.interrupt.requested:
                        drain_for_interrupt = True
            if first_error is not None:
                pool.cancel_pending()
                drain_for_interrupt = True

        if first_error is not None:
            raise RuntimeTaskError(
                f"frame-block task failed; remaining tasks were cancelled: {first_error!r}",
                {"error": repr(first_error), "last_frame_index": self._last_frame_index},
            ) from first_error
        if drain_for_interrupt:
            self._raise_if_interrupted(
                "frame_block",
                frame_index=self._last_frame_index,
                scene_id=self._last_scene_id,
            )
        self._last_frame_sequence = max(
            descriptor_sequences.values(), default=self._last_frame_sequence
        )

    def _load_complete_frames(
        self, descriptors: list[dict[str, Any]], recovery: dict[str, Any]
    ) -> list[dict[str, Any]]:
        errors = list(recovery["report"]["errors"])
        blocks = recovery["frame_blocks"]
        frames: list[dict[str, Any]] = []
        missing_blocks: list[str] = []
        for descriptor in descriptors:
            payload = blocks.get(descriptor["unit_key"])
            if payload is None or not self._block_matches_descriptor(
                payload, descriptor
            ):
                missing_blocks.append(descriptor["unit_key"])
                continue
            frames.extend(payload["frames"])
        if missing_blocks:
            errors.append(
                {
                    "code": "E_DIGEST",
                    "message": "one or more expected frame blocks are missing or invalid",
                    "details": {"missing_blocks": missing_blocks},
                }
            )
        if errors:
            self._emit(
                {
                    "event": "recovery-warnings",
                    "errors": errors,
                }
            )
        if missing_blocks:
            raise DigestError(
                "cannot assemble a complete frame timeline from snapshots",
                {"missing_blocks": missing_blocks},
            )
        frames.sort(key=lambda frame: frame["frame_index"])
        return frames

    def _write_replication_report(
        self, report: dict[str, Any], *, file_name: str = "replay_report.json"
    ) -> Path:
        path = self.run_output / file_name
        atomic_write_json(path, report)
        return path

    def _write_success_outputs(
        self,
        *,
        machine: SimulationKernel,
        frames: list[dict[str, Any]],
        replay_report: dict[str, Any],
        recovery_report: dict[str, Any],
    ) -> dict[str, Any]:
        self.run_output.mkdir(parents=True, exist_ok=True)
        ledger_doc = {
            "version_id": self.bundle.version_id,
            "run_id": self.bundle.run_id,
            "records": machine.ledger,
        }
        diagnostics_doc = {
            "version_id": self.bundle.version_id,
            "run_id": self.bundle.run_id,
            "fixed_scale": self.bundle.state.fixed_scale,
            "residual_tolerance": self.bundle.state.residual_tolerance_text,
            "fixed_is_authoritative": True,
            "reference_path_role": "diagnostic_only",
            "first_exceeded": machine.payload.get("first_exceeded"),
            "reference_mismatches": machine.payload.get("reference_mismatches", []),
            "points": machine.diagnostics,
            "summary": machine.summary(),
        }
        frame_doc = {
            "version_id": self.bundle.version_id,
            "run_id": self.bundle.run_id,
            "frame_count": len(frames),
            "frames": [
                {
                    "frame_index": frame["frame_index"],
                    "logical_time": frame["logical_time"],
                    "scene_id": frame["scene_id"],
                    "record_id": frame["record_id"],
                    "event_id": frame.get("event_id"),
                    "branch_id": frame.get("branch_id"),
                    "pixels_sha256": frame["pixels_sha256"],
                    "visible_markers": frame["visible_markers"],
                }
                for frame in frames
            ],
        }
        ledger_path = self.run_output / "ledger.json"
        diagnostics_path = self.run_output / "diagnostics.json"
        frames_path = self.run_output / "frames.json"
        recovery_path = self.run_output / "recovery_report.json"
        replay_path = self._write_replication_report(replay_report)
        atomic_write_json(ledger_path, ledger_doc)
        atomic_write_json(diagnostics_path, diagnostics_doc)
        atomic_write_json(frames_path, frame_doc)
        atomic_write_json(recovery_path, recovery_report)
        result = {
            "format": "NarrativeWorkbench.Result/1",
            "complete": True,
            "ok": True,
            "code": None,
            "version_id": self.bundle.version_id,
            "run_id": self.bundle.run_id,
            "fixed_scale": self.bundle.state.fixed_scale,
            "fixed_is_authoritative": True,
            "frame_count": len(frames),
            "ledger_record_count": len(machine.ledger),
            "replay_ok": replay_report["ok"],
            "recovery_error_count": len(recovery_report.get("errors", [])),
            "sim_snapshot_sequence": self._last_sim_sequence,
            "frame_block_snapshot_sequence": self._last_frame_sequence,
            "ledger_path": str(ledger_path.relative_to(self.run_output)),
            "diagnostics_path": str(diagnostics_path.relative_to(self.run_output)),
            "frames_path": str(frames_path.relative_to(self.run_output)),
            "replay_report_path": str(replay_path.relative_to(self.run_output)),
            "recovery_report_path": str(
                recovery_path.relative_to(self.run_output)
            ),
            "summary": machine.summary(),
        }
        atomic_write_json(self.result_path, result)
        return result

    def run(self) -> dict[str, Any]:
        self._validate_budget()
        archive_events = self.store.prepare()
        recovery = self.store.recover(archive_version_events=archive_events)
        recovery = self._prepare_run_root(recovery)
        if recovery.get("sim_entry") is not None:
            self._last_sim_sequence = int(recovery["sim_entry"]["sequence"])
            sim_payload = recovery.get("sim_payload") or {}
            sim_ledger = sim_payload.get("ledger", [])
            if sim_ledger:
                self._last_frame_index = sim_ledger[-1]["frame_index"]
                self._last_scene_id = sim_ledger[-1]["scene_id"]
        frame_entries = recovery.get("frame_entries", {})
        if frame_entries:
            self._last_frame_sequence = max(
                int(entry["sequence"]) for entry in frame_entries.values()
            )
        self.run_output.mkdir(parents=True, exist_ok=True)
        self.result_path.unlink(missing_ok=True)
        interrupt_marker = self.store.read_interrupt()
        if interrupt_marker and interrupt_marker.get("code") == "E_DIGEST":
            recovery["report"]["errors"].append(interrupt_marker)
        self._emit(
            {
                "event": "recovery",
                "report": recovery["report"],
                "interrupt_marker": interrupt_marker,
                "output_dir": str(self.run_output),
            }
        )

        machine = SimulationKernel(self.bundle, recovery["sim_payload"])
        pool = BoundedTaskPool(self.config.workers)
        memory = MemoryBudget(self.config.memory_budget_bytes)
        try:
            machine = self._run_simulation(
                machine, pool, memory, recovery["report"]
            )
            self._raise_if_interrupted(
                "event",
                frame_index=self._last_frame_index,
                scene_id=self._last_scene_id,
            )
            self._render_missing_frames(
                machine,
                pool,
                memory,
                recovery["frame_blocks"],
                recovery["report"],
            )
            self._raise_if_interrupted(
                "frame_block",
                frame_index=self._last_frame_index,
                scene_id=self._last_scene_id,
            )
        except Interrupted as exc:
            marker = self.store.write_interrupt(
                signal_name=exc.signal_name,
                boundary=exc.boundary,
                exit_code=exc.exit_code,
                frame_index=exc.frame_index,
                scene_id=exc.scene_id,
                snapshot_sequence=self._last_sim_sequence
                if exc.boundary == "event"
                else self._last_frame_sequence,
                test_injected=exc.test_injected,
            )
            self._emit({"event": "interrupted", "marker": marker})
            raise
        finally:
            pool.shutdown(wait_for_tasks=True)

        frames = build_frame_specs(self.bundle, machine.ledger)
        descriptors = plan_frame_blocks(
            self.bundle, frames, self.config.frame_block_size
        )
        final_recovery = self.store.recover(
            archive_version_events=recovery["report"].get("archive_events", [])
        )
        if recovery["report"].get("errors"):
            final_recovery["report"]["errors"] = (
                list(recovery["report"]["errors"])
                + final_recovery["report"]["errors"]
            )
        frames_payloads = self._load_complete_frames(descriptors, final_recovery)
        replay_report = verify_replay(self.bundle, machine.ledger, frames_payloads)
        if not replay_report["ok"]:
            self._write_replication_report(replay_report)
            ensure_replay_ok(replay_report)
        result = self._write_success_outputs(
            machine=machine,
            frames=frames_payloads,
            replay_report=replay_report,
            recovery_report=final_recovery["report"],
        )
        self.store.clear_interrupt()
        self._emit(
            {
                "event": "complete",
                "result": result,
                "replay_report": replay_report,
                "summary": machine.summary(),
            }
        )
        return result

