"""Audit orchestration, real-resource budgets, and report assembly."""

from __future__ import annotations

import hashlib
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from .adaptive import AdaptivePolicy, decide_policy
from .checkpoint import CheckpointLoad, load_checkpoint, write_checkpoint
from .config import AuditConfig
from .constants import (
    ALGORITHMS,
    REPORT_SCHEMA_VERSION,
    STAGES,
)
from .derivation import DerivationResult, evaluate_chains
from .errors import (
    E_BUDGET,
    E_FORMAT,
    E_INCOMPLETE,
    E_IO,
    E_TIMEOUT,
)
from .models import Event, Issue, ParsedFile, SessionData
from .normalization import normalize_sessions
from .parser import parse_bytes
from .state_machine import StateResult, evaluate_state_machine


CheckpointCallback = Callable[[dict[str, object]], None]


def _stage_sort_key(issue: Issue) -> tuple[int, int, int, str, str]:
    stage_index = STAGES.index(issue.stage) if issue.stage in STAGES else len(STAGES)
    return (
        stage_index,
        issue.seq if issue.seq is not None else -1,
        issue.offset if issue.offset is not None else -1,
        issue.code,
        issue.message,
    )


def _sorted_issues(issues: list[Issue] | tuple[Issue, ...]) -> list[Issue]:
    return sorted(issues, key=_stage_sort_key)


def _event_public(event: Event) -> dict[str, object]:
    return {
        "record_index": event.record_index,
        "offset": event.offset,
        "sync_marker": event.sync_marker,
        "stage": event.stage,
        "algorithm": event.algorithm,
        "stage_seq": event.seq,
        "params": event.params(),
        "salt_sha256": hashlib.sha256(event.salt).hexdigest(),
    }


def _stage_report(
    events: list[Event],
    issues: list[Issue],
    *,
    state_ran: bool,
) -> list[dict[str, object]]:
    stages: list[dict[str, object]] = []
    for stage in STAGES:
        stage_events = [event for event in events if event.stage == stage]
        stage_issues = [issue for issue in issues if issue.stage == stage]
        if stage_issues:
            status = "error"
        elif not state_ran:
            status = "not_run"
        elif not stage_events:
            status = "missing"
        else:
            status = "ok"
        stages.append(
            {
                "stage": stage,
                "stage_seqs": sorted(event.seq for event in stage_events),
                "status": status,
                "error_codes": sorted({issue.code for issue in stage_issues}),
            }
        )
    return stages


class AuditEngine:
    """Deterministic KLP batch auditor with optional checkpointing."""

    def __init__(
        self,
        config: AuditConfig | None = None,
        *,
        checkpoint_path: str | Path | None = None,
        workspace: str | Path | None = None,
        on_checkpoint: CheckpointCallback | None = None,
    ):
        self.config = config or AuditConfig()
        self.config.validate()
        self.checkpoint_path = Path(checkpoint_path) if checkpoint_path is not None else None
        self.workspace = Path(workspace) if workspace is not None else Path.cwd()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.lock_path = self.workspace / ".klp-workspace.lock"
        self.on_checkpoint = on_checkpoint

    def audit_file(self, path: str | Path) -> dict[str, object]:
        input_path = Path(path)
        try:
            data = input_path.read_bytes()
        except OSError as exc:
            from .errors import E_INPUT, KlpInputError

            raise KlpInputError(E_INPUT, f"cannot read input file {input_path}: {exc}") from exc
        return self.audit_bytes(data, source_name=str(input_path))

    def audit_bytes(self, data: bytes, *, source_name: str = "<memory>") -> dict[str, object]:
        parsed = parse_bytes(data)
        sessions = normalize_sessions(parsed.events)
        derive_keys = [
            (event.session_id, event.seq, event.record_index)
            for session in sessions
            for event in session.events
            if event.stage == "derive"
        ]
        policy = decide_policy(
            input_bytes=parsed.data_size,
            derive_keys=derive_keys,
            config=self.config,
        )

        checkpoint_load = CheckpointLoad(
            enabled=False,
            resumed=False,
            rebuilt=False,
            reason="disabled",
            reused_sessions={},
            previous_processed_count=0,
            previous_watermark=None,
            previous_complete=None,
        )
        if self.checkpoint_path is not None:
            if not parsed.ok:
                checkpoint_load = CheckpointLoad(
                    enabled=True,
                    resumed=False,
                    rebuilt=False,
                    reason="file_format_error",
                    reused_sessions={},
                    previous_processed_count=0,
                    previous_watermark=None,
                    previous_complete=None,
                )
            else:
                checkpoint_load = load_checkpoint(
                    self.checkpoint_path,
                    config=self.config,
                    source_sha256=parsed.file_sha256,
                    source_size=parsed.data_size,
                    expected_session_ids=[session.session_id for session in sessions],
                )

        reused_ids = set(checkpoint_load.reused_sessions)
        results: list[dict[str, object]] = [
            checkpoint_load.reused_sessions[session.session_id]
            for session in sessions
            if session.session_id in reused_ids
        ]
        remaining = [session for session in sessions if session.session_id not in reused_ids]

        if parsed.ok and remaining:
            results.extend(
                self._run_sessions(
                    remaining,
                    policy=policy,
                    parsed=parsed,
                    existing_results=results,
                    checkpoint_load=checkpoint_load,
                )
            )
        elif parsed.ok and self.checkpoint_path is not None and not checkpoint_load.reused_sessions:
            write_checkpoint(
                self.checkpoint_path,
                config=self.config,
                source_sha256=parsed.file_sha256,
                source_size=parsed.data_size,
                session_results=results,
                complete=True,
                lock_path=self.lock_path,
            )
            self._notify_checkpoint(results, complete=True)

        results.sort(key=lambda result: str(result["session_id"]))
        checkpoint_public = checkpoint_load.public_dict()
        if self.checkpoint_path is not None:
            checkpoint_public["current_watermark"] = (
                str(results[-1]["session_id"]) if results else None
            )
            checkpoint_public["current_processed_count"] = len(results)
            checkpoint_public["complete"] = (
                parsed.ok and len(results) == len(sessions)
            )

        return self._build_report(
            parsed=parsed,
            source_name=source_name,
            sessions=sessions,
            results=results,
            policy=policy,
            checkpoint=checkpoint_public,
        )

    def _run_sessions(
        self,
        sessions: list[SessionData],
        *,
        policy: AdaptivePolicy,
        parsed: ParsedFile,
        existing_results: list[dict[str, object]],
        checkpoint_load: CheckpointLoad,
    ) -> list[dict[str, object]]:
        new_results: list[dict[str, object]] = []
        new_since_checkpoint = 0
        batch_size = self.config.concurrency
        executor = ThreadPoolExecutor(max_workers=batch_size, thread_name_prefix="klp-audit")
        try:
            for start in range(0, len(sessions), batch_size):
                batch = sessions[start : start + batch_size]
                futures = [
                    executor.submit(self._audit_session, session, policy) for session in batch
                ]
                batch_results = [future.result() for future in futures]
                new_results.extend(batch_results)
                new_since_checkpoint += len(batch_results)
                final_batch = start + batch_size >= len(sessions)

                if (
                    self.checkpoint_path is not None
                    and (new_since_checkpoint >= self.config.checkpoint_interval or final_batch)
                ):
                    all_results = existing_results + new_results
                    write_checkpoint(
                        self.checkpoint_path,
                        config=self.config,
                        source_sha256=parsed.file_sha256,
                        source_size=parsed.data_size,
                        session_results=all_results,
                        complete=final_batch,
                        lock_path=self.lock_path,
                    )
                    new_since_checkpoint = 0
                    self._notify_checkpoint(all_results, complete=final_batch)
        finally:
            executor.shutdown(wait=True, cancel_futures=False)
        return new_results

    def _notify_checkpoint(
        self, results: list[dict[str, object]], *, complete: bool
    ) -> None:
        if self.on_checkpoint is None:
            return
        self.on_checkpoint(
            {
                "processed_count": len(results),
                "watermark_session_id": str(results[-1]["session_id"]) if results else None,
                "complete": complete,
            }
        )

    def _audit_session(
        self, session: SessionData, policy: AdaptivePolicy
    ) -> dict[str, object]:
        start = time.perf_counter()
        deadline = (
            start + float(self.config.session_timeout_seconds)
            if self.config.session_timeout_seconds is not None
            else None
        )
        issues: list[Issue] = list(session.normalization_issues)
        state_result: StateResult | None = None
        derivation_result: DerivationResult | None = None
        derivation_status = "not_run"
        derivation_completed = False
        budget_issue: Issue | None = None
        timeout_issue: Issue | None = None
        derive_events = [event for event in session.events if event.stage == "derive"]

        try:
            if deadline is not None and time.perf_counter() > deadline:
                raise TimeoutError("session deadline expired before validation")
            state_result = evaluate_state_machine(
                session.session_id, session.events, deadline=deadline
            )
            issues.extend(state_result.issues)
            if len(derive_events) > self.config.max_derivations_per_session:
                budget_issue = Issue(
                    code=E_BUDGET,
                    message=(
                        f"session {session.session_id!r} observed {len(derive_events)} "
                        "derived objects, exceeding the configured per-session limit"
                    ),
                    stage="derive",
                    seq=derive_events[self.config.max_derivations_per_session].seq,
                    offset=derive_events[self.config.max_derivations_per_session].offset,
                    details={
                        "observed_derived_objects": len(derive_events),
                        "max_derivations_per_session": self.config.max_derivations_per_session,
                    },
                )
                issues.append(budget_issue)
                derivation_status = "not_run"
            else:
                derivation_result = evaluate_chains(
                    session.session_id,
                    derive_events,
                    policy.selected_keys,
                    deadline=deadline,
                )
                issues.extend(derivation_result.issues)
                derivation_completed = derivation_result.completed
                if derivation_result.issues:
                    derivation_status = "fail"
                elif policy.degraded:
                    derivation_status = (
                        "degraded_pass"
                        if derivation_result.checked_nodes > 0
                        else "not_sampled"
                    )
                else:
                    derivation_status = "pass"
            if deadline is not None and time.perf_counter() > deadline:
                raise TimeoutError("session deadline expired after validation")
        except TimeoutError:
            elapsed = time.perf_counter() - start
            timeout_issue = Issue(
                code=E_TIMEOUT,
                message=(
                    f"session {session.session_id!r} exceeded its real-time budget "
                    f"({elapsed:.6f}s > {self.config.session_timeout_seconds}s)"
                ),
                offset=session.events[0].offset if session.events else None,
                details={
                    "elapsed_seconds": round(elapsed, 6),
                    "timeout_seconds": self.config.session_timeout_seconds,
                },
            )
            issues.append(timeout_issue)
            derivation_status = "timeout"
            derivation_completed = False
        except Exception as exc:
            issues.append(
                Issue(
                    code=E_IO,
                    message=f"session validation failed unexpectedly: {exc}",
                    offset=session.events[0].offset if session.events else None,
                )
            )
            derivation_status = "error"
            derivation_completed = False

        issues = _sorted_issues(issues)
        state_ran = state_result is not None
        stages = _stage_report(session.events, issues, state_ran=state_ran)
        error_codes = sorted({issue.code for issue in issues})
        if issues:
            status = "fail"
        elif policy.degraded:
            status = "degraded_pass"
        else:
            status = "pass"

        derivation_public: dict[str, object] = {
            "status": derivation_status,
            "mode": policy.mode,
            "completed": derivation_completed,
            "chain_count": derivation_result.chain_count if derivation_result else 0,
            "total_nodes": (
                derivation_result.total_nodes if derivation_result else len(derive_events)
            ),
            "checked_nodes": derivation_result.checked_nodes if derivation_result else 0,
            "coverage_ratio": (
                round(derivation_result.checked_nodes / derivation_result.total_nodes, 8)
                if derivation_result and derivation_result.total_nodes
                else (1.0 if derivation_result else 0.0)
            ),
        }
        if budget_issue is not None:
            derivation_public["reason"] = E_BUDGET
        elif timeout_issue is not None:
            derivation_public["reason"] = E_TIMEOUT

        return {
            "session_id": session.session_id,
            "status": status,
            "error_codes": error_codes,
            "deduplicated_records": session.deduplicated_count,
            "events": [_event_public(event) for event in session.events],
            "stages": stages,
            "checks": {
                "state_machine": (
                    "not_run"
                    if state_result is None
                    else ("pass" if state_result.passed else "fail")
                ),
                "derivation": derivation_public,
            },
            "issues": [issue.public_dict() for issue in issues],
        }

    def _build_report(
        self,
        *,
        parsed: ParsedFile,
        source_name: str,
        sessions: list[SessionData],
        results: list[dict[str, object]],
        policy: AdaptivePolicy,
        checkpoint: dict[str, object],
    ) -> dict[str, object]:
        file_issues: list[Issue] = []
        if parsed.fatal_code is not None:
            file_issues.append(
                Issue(
                    code=parsed.fatal_code,
                    message=parsed.fatal_message or "file format error",
                )
            )
        elif parsed.record_count == 0:
            file_issues.append(
                Issue(
                    code=E_INCOMPLETE,
                    message="KLP file contains no session records",
                )
            )

        passed = sum(result.get("status") == "pass" for result in results)
        degraded = sum(result.get("status") == "degraded_pass" for result in results)
        failed = sum(result.get("status") == "fail" for result in results)
        session_codes = {
            str(code)
            for result in results
            for code in result.get("error_codes", ())
        }
        rejected_codes = {rejected.code for rejected in parsed.rejected}
        all_codes = sorted(
            {issue.code for issue in file_issues} | session_codes | rejected_codes
        )

        if file_issues or parsed.rejected or failed:
            conclusion = "FAIL"
        elif degraded:
            conclusion = "DEGRADED_PASS"
        else:
            conclusion = "PASS"

        return {
            "schema_version": REPORT_SCHEMA_VERSION,
            "tool": {
                "name": "klp-audit",
                "version": "1.0.0",
            },
            "file": {
                "source": source_name,
                "sha256": parsed.file_sha256,
                "size": parsed.data_size,
                "max_iterations": parsed.max_iterations,
                "declared_record_count": parsed.record_count,
                "accepted_record_count": len(parsed.events),
                "rejected_record_count": len(parsed.rejected),
                "conclusion": conclusion,
                "error_codes": sorted({issue.code for issue in file_issues}),
                "errors": [issue.public_dict() for issue in file_issues],
            },
            "policy": {
                "algorithms": list(ALGORITHMS),
                "algorithm_policy_version": self.config.algorithm_policy_version,
                "config": self.config.public_dict(),
            },
            "adaptive": policy.public_dict(),
            "checkpoint": checkpoint,
            "summary": {
                "sessions": len(results),
                "passed": passed,
                "degraded_pass": degraded,
                "failed": failed,
                "rejected_records": len(parsed.rejected),
                "business_error_codes": all_codes,
            },
            "sessions": results,
            "rejected_records": [
                rejected.public_dict() for rejected in parsed.rejected
            ],
        }


def report_exit_code(report: dict[str, object]) -> int:
    file_section = report.get("file", {})
    if isinstance(file_section, dict) and file_section.get("conclusion") == "PASS":
        return 0
    return 1


def report_error_lines(report: dict[str, object]) -> list[str]:
    lines: list[str] = []
    file_section = report.get("file", {})
    if isinstance(file_section, dict):
        for error in file_section.get("errors", []):
            if isinstance(error, dict):
                lines.append(f"{error.get('code')}: {error.get('message')}")
    for rejected in report.get("rejected_records", []):
        if isinstance(rejected, dict):
            lines.append(
                f"{rejected.get('code')}: record {rejected.get('record_index')}: "
                f"{rejected.get('message')}"
            )
    sessions = report.get("sessions", [])
    if isinstance(sessions, list):
        for session in sessions:
            if not isinstance(session, dict):
                continue
            for issue in session.get("issues", []):
                if isinstance(issue, dict):
                    lines.append(
                        f"{issue.get('code')}: session {session.get('session_id')}: "
                        f"{issue.get('message')}"
                    )
    return lines

