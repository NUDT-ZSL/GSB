"""Deterministic constraint solving with rollback and resumable snapshots."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from .constraints import CandidateRejected, apply_constraint
from .errors import EXIT_INTERRUPTED, SolverError
from .grid import BLOCKED, EMPTY, LevelState
from .model import LevelSpec
from .preflight import PreflightReport, memory_admission, preflight
from .snapshots import (
    IntegrityIssue,
    LoadedSnapshot,
    ResumeInfo,
    SnapshotStore,
)


class SolverInterrupted(Exception):
    def __init__(
        self,
        *,
        marker_path: Path,
        applied_count: int,
        version_id: str | None,
        constraint_id: str | None,
        warnings: Sequence[IntegrityIssue] = (),
    ) -> None:
        super().__init__("solver interrupted at a constraint boundary")
        self.marker_path = marker_path
        self.applied_count = applied_count
        self.version_id = version_id
        self.constraint_id = constraint_id
        self.warnings = tuple(warnings)
        self.exit_code = EXIT_INTERRUPTED

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "status": "interrupted",
            "error": {
                "code": "E_INTERRUPTED",
                "message": "solver interrupted at a constraint boundary",
                "details": {
                    "marker": str(self.marker_path),
                    "applied_count": self.applied_count,
                    "version_id": self.version_id,
                    "next_constraint_id": self.constraint_id,
                    "warnings": [warning.as_dict() for warning in self.warnings],
                },
            },
            "exit_code": self.exit_code,
        }


@dataclass
class ConstraintEvent:
    constraint_id: str
    attempt: int
    accepted: bool
    preflight: dict[str, Any]
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "constraint_id": self.constraint_id,
            "attempt": self.attempt,
            "accepted": self.accepted,
            "preflight": self.preflight,
            "error": self.error,
        }


@dataclass
class SolveStats:
    candidate_attempts: int = 0
    rollbacks: int = 0
    preflight_workers_max: int = 0
    preflight_cancelled: int = 0
    candidate_states_max: int = 1
    events: list[ConstraintEvent] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidate_attempts": self.candidate_attempts,
            "rollbacks": self.rollbacks,
            "preflight_workers_max": self.preflight_workers_max,
            "preflight_cancelled": self.preflight_cancelled,
            "candidate_states_max": self.candidate_states_max,
            "preflight_events": [event.as_dict() for event in self.events],
        }


@dataclass
class SolveResult:
    spec: LevelSpec
    state: LevelState
    snapshots: list[LoadedSnapshot]
    stats: SolveStats
    resume: ResumeInfo
    conflict_pairs: list[dict[str, str]]

    @property
    def latest_version_id(self) -> str | None:
        return self.snapshots[-1].version_id if self.snapshots else None

    def as_dict(self) -> dict[str, Any]:
        warnings = [warning.as_dict() for warning in self.resume.warnings]
        return {
            "ok": True,
            "status": "completed",
            "exit_code": 0,
            "width": self.spec.width,
            "height": self.spec.height,
            "seed": self.spec.seed,
            "spec_digest": self.spec.spec_digest,
            "run_id": self.spec.run_id,
            "version_id": self.latest_version_id,
            "grid": [list(row) for row in self.state.grid],
            "ascii_grid": render_ascii(self.state),
            "rooms": [room.canonical() for room in self.state.rooms],
            "applied_count": len(self.spec.constraints),
            "constraint_count": len(self.spec.constraints),
            "constraint_ids": self.spec.constraint_ids(),
            "constraint_application_count": len(self.spec.constraints),
            "candidate_attempts": self.stats.candidate_attempts,
            "rollback_count": self.stats.rollbacks,
            "conflicts": self.conflict_pairs,
            "unresolved_constraints": [],
            "snapshot_count": len(self.snapshots),
            "snapshot_versions": [
                {
                    "applied_count": snapshot.applied_count,
                    "version_id": snapshot.version_id,
                    "digest": snapshot.digest,
                    "file": snapshot.path.name,
                }
                for snapshot in self.snapshots
            ],
            "resume": {
                "source": self.resume.source,
                "start_index": self.resume.start_index,
                "restarted": self.resume.restarted,
                "snapshots_checked": self.resume.snapshots_checked,
                "warnings": warnings,
            },
            "preflight": self.stats.as_dict(),
            "warnings": warnings,
        }


def render_ascii(state: LevelState) -> list[str]:
    symbols = {BLOCKED: "#", EMPTY: ".", 1: "R", 2: "+"}
    return ["".join(symbols[value] for value in row) for row in state.grid]


class LevelSolver:
    """Solve one parsed specification and persist recoverable snapshots."""

    def __init__(
        self,
        spec: LevelSpec,
        snapshot_dir: str | Path,
        *,
        fresh: bool = False,
        strict_digest: bool = False,
        interrupt_requested: Callable[[], bool] | None = None,
        interrupt_after: int | None = None,
    ) -> None:
        self.spec = spec
        self.snapshot_dir = Path(snapshot_dir)
        self.fresh = fresh
        self.strict_digest = strict_digest
        self.interrupt_requested = interrupt_requested or (lambda: False)
        self.interrupt_after = interrupt_after

    def solve(self) -> SolveResult:
        # Resource admission happens before the store is touched.
        memory_admission(self.spec)
        store = SnapshotStore(self.snapshot_dir, self.spec)
        resume = store.prepare(fresh=self.fresh, strict_digest=self.strict_digest)
        if resume.start_index > 0 and not resume.history_complete:
            # A partial snapshot chain cannot support deterministic rollback.
            # Preserve it and then rebuild from the deterministic initial state.
            previous_warnings = list(resume.warnings)
            store.archive_active("incomplete_resume_chain")
            resume = store.prepare(fresh=True, strict_digest=False)
            resume.warnings = previous_warnings + [
                IntegrityIssue(
                    "E_VERSION",
                    "snapshot ancestry was incomplete; restarted from scratch",
                    "snapshots",
                )
            ] + resume.warnings
            resume.restarted = True

        initial = LevelState.initial(
            self.spec.width, self.spec.height, self.spec.blocked
        )
        base_states: list[LevelState] = [initial]
        committed: list[LoadedSnapshot] = list(resume.chain)
        for snapshot in resume.chain:
            base_states.append(snapshot.state)
        if len(base_states) != resume.start_index + 1:
            # The checked chain has exactly one snapshot per applied constraint.
            base_states = [initial] + [snapshot.state for snapshot in resume.chain]

        trace = list(resume.selected_attempts)
        if len(trace) != resume.start_index:
            raise SolverError(
                "E_DIGEST",
                "resume metadata does not contain one attempt per applied constraint",
            )
        state = resume.state
        rollback_count = resume.rollback_count
        attempts_total = resume.attempts_total
        current_index = resume.start_index
        next_attempt = 0
        latest = committed[-1] if committed else None
        persisted_latest = latest
        conflict_pairs: list[dict[str, str]] = []
        stats = SolveStats(rollbacks=rollback_count)
        safe_to_interrupt = True
        last_errors: list[dict[str, str]] = []

        while current_index < len(self.spec.constraints):
            if safe_to_interrupt and self._interrupt_requested(current_index):
                self._interrupt(
                    store,
                    persisted_latest,
                    current_index=current_index,
                    rollback_count=rollback_count,
                    attempts_total=attempts_total,
                    warnings=resume.warnings,
                )

            if current_index >= len(base_states):
                raise SolverError(
                    "E_INTERNAL",
                    "solver state history is inconsistent",
                    {"current_index": current_index, "history_size": len(base_states)},
                )
            base_state = base_states[current_index]
            constraint = self.spec.constraints[current_index]
            if next_attempt >= self.spec.config.attempt_limit:
                unresolved = constraint.id
                stats.events.append(
                    ConstraintEvent(
                        constraint_id=unresolved,
                        attempt=next_attempt - 1,
                        accepted=False,
                        preflight={},
                        error="attempt limit exhausted",
                    )
                )
                if not self._backtrack(
                    current_index=current_index,
                    trace=trace,
                    base_states=base_states,
                    committed=committed,
                    rollback_count=rollback_count,
                    attempt_limit=self.spec.config.attempt_limit,
                    rollback_limit=self.spec.config.rollback_limit,
                ):
                    self._raise_conflict(
                        store,
                        persisted_latest,
                        unresolved=unresolved,
                        conflict_pairs=conflict_pairs,
                        last_errors=last_errors,
                        rollback_count=rollback_count,
                        attempts_total=attempts_total,
                    )
                (
                    current_index,
                    next_attempt,
                    trace,
                    base_states,
                    committed,
                    rollback_count,
                    latest,
                    state,
                ) = self._apply_backtrack(
                    current_index=current_index,
                    trace=trace,
                    base_states=base_states,
                    committed=committed,
                    rollback_count=rollback_count,
                )
                stats.rollbacks = rollback_count
                safe_to_interrupt = False
                continue

            try:
                candidate = apply_constraint(
                    self.spec, constraint, base_state, next_attempt
                )
            except CandidateRejected as exc:
                attempts_total += 1
                stats.candidate_attempts = attempts_total
                pair = self._conflict_pair(
                    constraint.id, exc.conflict_ids, current_index
                )
                if pair is not None:
                    conflict_pairs.append(pair)
                last_errors = [
                    {
                        "constraint_id": constraint.id,
                        "message": exc.message,
                    }
                ]
                stats.events.append(
                    ConstraintEvent(
                        constraint_id=constraint.id,
                        attempt=next_attempt,
                        accepted=False,
                        preflight={},
                        error=exc.message,
                    )
                )
                next_attempt += 1
                continue

            report = preflight(
                self.spec, candidate, self.spec.constraints[: current_index + 1]
            )
            attempts_total += 1
            stats.candidate_attempts = attempts_total
            stats.preflight_workers_max = max(
                stats.preflight_workers_max, report.workers
            )
            stats.preflight_cancelled += report.cancelled
            event = ConstraintEvent(
                constraint_id=constraint.id,
                attempt=next_attempt,
                accepted=report.ok,
                preflight=report.as_dict(),
                error=(
                    report.errors[0].message
                    if not report.ok and report.errors
                    else None
                ),
            )
            stats.events.append(event)

            if not report.ok:
                for violation in report.errors:
                    pair = self._conflict_pair(
                        constraint.id, (violation.constraint_id,), current_index
                    )
                    if pair is not None:
                        conflict_pairs.append(pair)
                last_errors = [error.as_dict() for error in report.errors]
                next_attempt += 1
                continue

            selected_attempts = [*trace, next_attempt]
            snapshot = store.write_snapshot(
                candidate,
                applied_count=current_index + 1,
                selected_attempts=selected_attempts,
                rollback_count=rollback_count,
                attempts_total=attempts_total,
                parent=committed[-1] if committed else None,
            )
            store.update_manifest(
                snapshot,
                rollback_count=rollback_count,
                attempts_total=attempts_total,
            )
            archived = store.archive_unreachable([*committed, snapshot])
            if archived:
                event.preflight["superseded_snapshots_archived"] = archived

            trace.append(next_attempt)
            committed.append(snapshot)
            if len(base_states) > current_index + 1:
                base_states = base_states[: current_index + 1]
            base_states.append(candidate)
            latest = snapshot
            persisted_latest = snapshot
            state = candidate
            current_index += 1
            next_attempt = 0
            safe_to_interrupt = True

            if self._interrupt_requested(current_index):
                self._interrupt(
                    store,
                    persisted_latest,
                    current_index=current_index,
                    rollback_count=rollback_count,
                    attempts_total=attempts_total,
                    warnings=resume.warnings,
                )

        if self._interrupt_requested(current_index):
            self._interrupt(
                store,
                persisted_latest,
                current_index=current_index,
                rollback_count=rollback_count,
                attempts_total=attempts_total,
                warnings=resume.warnings,
            )

        store.mark_completed(
            persisted_latest,
            rollback_count=rollback_count,
            attempts_total=attempts_total,
        )
        stats.rollbacks = rollback_count
        stats.candidate_attempts = attempts_total
        return SolveResult(
            spec=self.spec,
            state=state,
            snapshots=committed,
            stats=stats,
            resume=resume,
            conflict_pairs=self._dedupe_pairs(conflict_pairs),
        )

    def _interrupt_requested(self, current_index: int) -> bool:
        if self.interrupt_requested():
            return True
        return (
            self.interrupt_after is not None
            and current_index >= self.interrupt_after
        )

    def _interrupt(
        self,
        store: SnapshotStore,
        latest: LoadedSnapshot | None,
        *,
        current_index: int,
        rollback_count: int,
        attempts_total: int,
        warnings: Sequence[IntegrityIssue],
    ) -> None:
        marker = store.write_interrupt(
            latest,
            rollback_count=rollback_count,
            attempts_total=attempts_total,
            applied_count=current_index,
            reason="SIGINT/SIGTERM or requested boundary stop",
        )
        next_id = (
            self.spec.constraints[current_index].id
            if current_index < len(self.spec.constraints)
            else None
        )
        raise SolverInterrupted(
            marker_path=marker,
            applied_count=current_index,
            version_id=latest.version_id if latest else None,
            constraint_id=next_id,
            warnings=warnings,
        )

    def _conflict_pair(
        self,
        current_id: str,
        earlier_ids: Sequence[str],
        current_index: int,
    ) -> dict[str, str] | None:
        earlier = ""
        for candidate in earlier_ids:
            if candidate and candidate != "<state>" and candidate != current_id:
                earlier = candidate
                break
        if not earlier and current_index > 0:
            earlier = self.spec.constraints[current_index - 1].id
        if not earlier:
            earlier = current_id
        return {"earlier": earlier, "later": current_id}

    @staticmethod
    def _dedupe_pairs(
        pairs: Sequence[dict[str, str]]
    ) -> list[dict[str, str]]:
        seen: set[tuple[str, str]] = set()
        result: list[dict[str, str]] = []
        for pair in pairs:
            key = (pair["earlier"], pair["later"])
            if key in seen:
                continue
            seen.add(key)
            result.append(pair)
        return result

    def _backtrack(
        self,
        *,
        current_index: int,
        trace: Sequence[int],
        base_states: Sequence[LevelState],
        committed: Sequence[LoadedSnapshot],
        rollback_count: int,
        attempt_limit: int,
        rollback_limit: int,
    ) -> bool:
        if current_index <= 0:
            return False
        target = current_index - 1
        while target >= 0 and trace[target] + 1 >= attempt_limit:
            target -= 1
        if target < 0:
            return False
        depth = current_index - target
        if depth > rollback_limit:
            return False
        if target >= len(base_states) or target >= len(trace):
            return False
        return True

    def _apply_backtrack(
        self,
        *,
        current_index: int,
        trace: list[int],
        base_states: list[LevelState],
        committed: list[LoadedSnapshot],
        rollback_count: int,
    ) -> tuple[
        int,
        int,
        list[int],
        list[LevelState],
        list[LoadedSnapshot],
        int,
        LoadedSnapshot | None,
        LevelState,
    ]:
        target = current_index - 1
        while target >= 0 and trace[target] + 1 >= self.spec.config.attempt_limit:
            target -= 1
        if target < 0:
            raise SolverError("E_INTERNAL", "backtrack target was not found")
        rollback_count += 1
        next_attempt = trace[target] + 1
        trace = trace[:target]
        base_states = base_states[: target + 1]
        committed = committed[:target]
        latest = committed[-1] if committed else None
        state = base_states[target]
        return (
            target,
            next_attempt,
            trace,
            base_states,
            committed,
            rollback_count,
            latest,
            state,
        )

    def _raise_conflict(
        self,
        store: SnapshotStore,
        persisted_latest: LoadedSnapshot | None,
        *,
        unresolved: str,
        conflict_pairs: Sequence[dict[str, str]],
        last_errors: Sequence[dict[str, str]],
        rollback_count: int,
        attempts_total: int,
    ) -> None:
        pairs = self._dedupe_pairs(conflict_pairs)
        store.mark_conflict(
            persisted_latest,
            rollback_count=rollback_count,
            attempts_total=attempts_total,
            unresolved=[unresolved],
        )
        raise SolverError(
            "E_CONFLICT",
            f"constraint {unresolved} cannot be satisfied within the rollback limit",
            {
                "unresolved_constraints": [unresolved],
                "conflict_pairs": pairs,
                "last_errors": list(last_errors),
                "rollback_count": rollback_count,
                "rollback_limit": self.spec.config.rollback_limit,
                "applied_count": persisted_latest.applied_count if persisted_latest else 0,
            },
        )
