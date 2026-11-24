from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .audio import atomic_write_bytes, atomic_write_json, write_wav_blocks_atomic
from .errors import (
    EXIT_GRACEFUL_INTERRUPT,
    EXIT_OK,
    EXIT_TASK_INVALID,
    EXIT_UNSCHEDULABLE,
    BusinessIssue,
    LedgerError,
    UnschedulableError,
)
from .ledger import Ledger
from .report import report_bytes
from .runtime import RecordingRuntime
from .scheduler import BlockScheduler, DeterministicLogicalClock, ScheduleResult
from .task import STAGE_NAMES, Task


@dataclass
class RunOptions:
    mode: str = "batch"
    workers: int | None = None
    crash_after_commits: int | None = None
    stop_event: threading.Event = field(default_factory=threading.Event)
    signal_number: Callable[[], int | None] = lambda: None
    clock: DeterministicLogicalClock | None = None


@dataclass
class RunOutcome:
    exit_code: int
    status: str
    report_path: Path
    report: dict[str, Any]
    run_id: str


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class BatchEngine:
    def __init__(
        self,
        *,
        task: Task,
        validation_issues: list[BusinessIssue],
        ledger_path: Path,
        output_dir: Path,
        report_path: Path,
        state_dir: Path,
        options: RunOptions,
    ) -> None:
        self.task = task
        self.validation_issues = list(validation_issues)
        self.ledger_path = ledger_path
        self.output_dir = output_dir
        self.report_path = report_path
        self.state_dir = state_dir
        self.options = options

    def run(self) -> RunOutcome:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.report_path.parent.mkdir(parents=True, exist_ok=True)
        ledger = Ledger(self.ledger_path)
        ledger.initialize_task(self.task.raw_sha256, self.task.input_digests())
        ledger.set_crash_after_commits(self.options.crash_after_commits)
        workers = self.options.workers or self.task.parallelism
        workers = max(1, min(workers, self.task.parallelism))
        run_info = ledger.begin_run(
            mode=self.options.mode,
            workers=workers,
            parallelism=self.task.parallelism,
        )
        status_payload = {
            "status": "running",
            "run_id": run_info.run_id,
            "task_id": self.task.task_id,
            "started_at": run_info.started_at,
            "ledger": str(self.ledger_path),
        }
        atomic_write_json(self.state_dir / "status.json", status_payload)

        runtimes: list[RecordingRuntime] = []
        issues = list(self.validation_issues)
        for spec in self.task.recordings:
            try:
                runtimes.append(RecordingRuntime(spec, ledger, run_info.run_id))
            except LedgerError as exc:
                issues.append(BusinessIssue(spec.recording_id, exc.code, exc.message))

        scheduler = BlockScheduler(
            task=self.task,
            runtimes=runtimes,
            workers=workers,
            stop_event=self.options.stop_event,
            signal_number=self.options.signal_number,
            clock=self.options.clock,
        )
        try:
            schedule_result = scheduler.run()
        except UnschedulableError as exc:
            issues.append(BusinessIssue("*", exc.code, exc.message))
            ledger.finish_run(run_info.run_id, "unschedulable")
            recordings = self._recording_reports(
                ledger,
                runtimes,
                run_info.run_id,
                run_info.is_resume,
                set(),
                finalize_outputs=False,
            )
            report = self._build_report(
                run_info.run_id,
                run_info.is_resume,
                workers,
                issues,
                scheduler,
                None,
                recordings,
                "unschedulable",
            )
            self._write_report_and_status(report, "unschedulable", EXIT_UNSCHEDULABLE)
            ledger.close()
            return RunOutcome(
                EXIT_UNSCHEDULABLE,
                "unschedulable",
                self.report_path,
                report,
                run_info.run_id,
            )
        except Exception:
            ledger.finish_run(run_info.run_id, "failed")
            ledger.close()
            raise

        issues.extend(schedule_result.issues)
        failed_records = {
            issue.recording_id for issue in schedule_result.issues if issue.recording_id != "*"
        }
        interrupted = schedule_result.interrupted or self.options.stop_event.is_set()
        if interrupted:
            ledger.finish_run(run_info.run_id, "interrupted")
            detail = "graceful stop requested at a block boundary"
            ledger.record_interruption(
                run_info.run_id,
                kind="graceful",
                signal_number=schedule_result.signal_number,
                detail=detail,
            )
            interrupted_marker = {
                "status": "interrupted",
                "run_id": run_info.run_id,
                "task_id": self.task.task_id,
                "signal_number": schedule_result.signal_number,
                "detail": detail,
                "created_at": _utc_now(),
            }
            atomic_write_json(self.state_dir / "interrupted.json", interrupted_marker)
            recordings = self._recording_reports(
                ledger,
                runtimes,
                run_info.run_id,
                run_info.is_resume,
                failed_records,
                finalize_outputs=False,
            )
            report = self._build_report(
                run_info.run_id,
                run_info.is_resume,
                workers,
                issues,
                scheduler,
                schedule_result,
                recordings,
                "interrupted",
            )
            self._write_report_and_status(report, "interrupted", EXIT_GRACEFUL_INTERRUPT)
            ledger.close()
            return RunOutcome(
                EXIT_GRACEFUL_INTERRUPT,
                "interrupted",
                self.report_path,
                report,
                run_info.run_id,
            )

        recordings = self._recording_reports(
            ledger,
            runtimes,
            run_info.run_id,
            run_info.is_resume,
            failed_records,
            finalize_outputs=True,
        )
        status = "completed" if not issues else "completed_with_errors"
        exit_code = EXIT_OK if not issues else EXIT_TASK_INVALID
        ledger.finish_run(run_info.run_id, status)
        report = self._build_report(
            run_info.run_id,
            run_info.is_resume,
            workers,
            issues,
            scheduler,
            schedule_result,
            recordings,
            status,
        )
        self._write_report_and_status(report, status, exit_code)
        ledger.close()
        return RunOutcome(
            exit_code,
            status,
            self.report_path,
            report,
            run_info.run_id,
        )

    def _recording_reports(
        self,
        ledger: Ledger,
        runtimes: list[RecordingRuntime],
        run_id: str,
        is_resume: bool,
        failed_records: set[str],
        *,
        finalize_outputs: bool,
    ) -> list[dict[str, Any]]:
        reports = []
        for runtime in sorted(runtimes, key=lambda item: item.recording_id):
            stage_reports = []
            run_counts = ledger.count_run_blocks_by_stage(run_id, runtime.recording_id)
            for stage in range(4):
                records = ledger.all_block_records(runtime.recording_id, stage)
                completed = len(records)
                total = runtime.expected_count(stage)
                record_is_resume = any(runtime.prior_counts.values())
                repaired = run_counts.get(stage, 0) if record_is_resume else 0
                stage_reports.append(
                    {
                        "stage": stage,
                        "name": STAGE_NAMES[stage],
                        "total_blocks": total,
                        "completed_blocks": completed,
                        "complete": completed == total,
                        "resume_repaired_blocks": repaired,
                        "block_sha256": [record.content_sha256 for record in records],
                    }
                )

            complete = all(stage["complete"] for stage in stage_reports)
            failed = runtime.recording_id in failed_records
            item: dict[str, Any] = {
                "recording_id": runtime.recording_id,
                "sample_rate": runtime.spec.sample_rate,
                "sample_count": runtime.sample_count,
                "input_sha256": runtime.spec.wav_info.sha256,
                "status": (
                    "complete"
                    if complete and not failed
                    else "failed"
                    if failed
                    else "incomplete"
                ),
                "stages": stage_reports,
                "spectral_overlap_max_abs_error": None,
                "normalization_gain": None,
                "output_path": None,
                "output_sha256": None,
                "output_changed": None,
            }
            if runtime.stage_complete(2):
                try:
                    item["spectral_overlap_max_abs_error"] = (
                        runtime.spectral_overlap_max_abs_error()
                    )
                    _, gain = runtime.ensure_denoise_metrics()
                    item["normalization_gain"] = gain
                except LedgerError:
                    pass
            if finalize_outputs and complete and not failed:
                output_path = self.output_dir / f"{runtime.recording_id}.wav"
                digest, changed = write_wav_blocks_atomic(
                    output_path,
                    runtime.iter_stage_audio(3),
                    runtime.sample_count,
                    runtime.spec.sample_rate,
                )
                item["output_path"] = str(output_path)
                item["output_sha256"] = digest
                item["output_changed"] = changed
            reports.append(item)
        return reports

    def _build_report(
        self,
        run_id: str,
        is_resume: bool,
        workers: int,
        issues: list[BusinessIssue],
        scheduler: BlockScheduler,
        schedule_result: ScheduleResult | None,
        recordings: list[dict[str, Any]],
        status: str,
    ) -> dict[str, Any]:
        issue_dicts = [issue.as_dict() for issue in issues]
        issue_dicts.sort(key=lambda item: (item["recording_id"], item["code"], item["message"]))
        total_blocks = sum(
            stage["total_blocks"]
            for recording in recordings
            for stage in recording["stages"]
        )
        completed_blocks = sum(
            stage["completed_blocks"]
            for recording in recordings
            for stage in recording["stages"]
        )
        round_dicts = (
            [round_record.as_dict() for round_record in schedule_result.rounds]
            if schedule_result is not None
            else []
        )
        deferred_blocks = (
            schedule_result.deferred_count if schedule_result is not None else 0
        )
        report = {
            "schema_version": 1,
            "task_id": self.task.task_id,
            "task_sha256": self.task.raw_sha256,
            "generated_at": _utc_now(),
            "status": status,
            "run": {
                "run_id": run_id,
                "is_resume": is_resume,
                "mode": self.options.mode,
                "workers": workers,
                "parallelism": self.task.parallelism,
            },
            "budgets": {
                "round_time_budget_ns": self.task.round_time_budget_ns,
                "memory_budget_bytes": self.task.memory_budget_bytes,
            },
            "business_codes": issue_dicts,
            "recordings": recordings,
            "scheduling": {
                "clock_ns": scheduler.clock.now_ns(),
                "rounds": round_dicts,
                "deferred_blocks": deferred_blocks,
            },
            "summary": {
                "total_blocks": total_blocks,
                "completed_blocks": completed_blocks,
                "deferred_blocks": deferred_blocks,
                "rounds": len(round_dicts),
                "valid_recordings": len(self.task.recordings),
                "business_code_count": len(issue_dicts),
            },
        }
        return report

    def _write_report_and_status(
        self,
        report: dict[str, Any],
        status: str,
        exit_code: int,
    ) -> None:
        atomic_write_bytes(self.report_path, report_bytes(report))
        atomic_write_json(
            self.state_dir / "status.json",
            {
                "status": status,
                "exit_code": exit_code,
                "finished_at": _utc_now(),
                "task_id": self.task.task_id,
                "run_id": report["run"]["run_id"],
                "report": str(self.report_path),
                "summary": report["summary"],
            },
        )
        if status == "completed" or status == "completed_with_errors":
            (self.state_dir / "interrupted.json").unlink(missing_ok=True)
