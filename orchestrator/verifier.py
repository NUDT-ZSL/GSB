"""Independent verification of a session snapshot against stage outputs."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Dict, List, Optional

from .dag_loader import parse_dag
from .errors import VerifyError
from .executor import hash_output_file
from .models import TraceEvent
from .snapshot import Snapshot


@dataclass
class VerificationReport:
    ok: bool
    checks: dict
    deviations: List[dict]

    def to_dict(self) -> dict:
        return {
            "code": "OK" if self.ok else "E_VERIFY",
            "ok": self.ok,
            "checks": self.checks,
            "deviations": self.deviations,
        }


def _deviation(stage_id: str, check: str, reason: str, evidence: Optional[dict] = None) -> dict:
    out = {"stage_id": stage_id, "check": check, "reason": reason}
    if evidence:
        out["evidence"] = evidence
    return out


def verify_snapshot(snapshot: Snapshot, output_root: str) -> VerificationReport:
    deviations: List[dict] = []
    dag = parse_dag(snapshot.dag_definition)
    states = snapshot.states
    trace = sorted(snapshot.trace, key=lambda event: event.seq)

    _check_dependency_order(dag, states, trace, deviations)
    peak_used = _check_resource_budget(dag, snapshot, trace, deviations)
    _check_output_digests(dag, states, output_root, deviations)
    _check_skip_basis(dag, states, deviations)
    _check_weak_annotations(dag, states, deviations)
    _check_terminal_coverage(dag, states, deviations)

    checks = {
        "strong_dependency_order": "passed",
        "resource_budget": {
            "budget": snapshot.global_resource_budget,
            "peak_observed": peak_used,
            "status": "passed",
        },
        "output_digests": "passed",
        "skip_basis": "passed",
        "weak_upstream_annotation": "passed",
        "terminal_coverage": "passed",
    }
    failed_checks = sorted({item["check"] for item in deviations})
    for name in list(checks):
        if name in failed_checks:
            if isinstance(checks[name], dict):
                checks[name]["status"] = "failed"
            else:
                checks[name] = "failed"
    deviations.sort(key=lambda item: (item["stage_id"], item["check"], item["reason"]))
    return VerificationReport(ok=not deviations, checks=checks, deviations=deviations)


def _success_interval(states, stage_id: str):
    result = states.get(stage_id)
    if result is None or result.state != "succeeded":
        return None
    return result.start_mono_ms, result.end_mono_ms


def _check_dependency_order(dag, states, trace: List[TraceEvent], deviations: List[dict]) -> None:
    for stage_id in dag.stage_ids_sorted():
        stage = dag.stages[stage_id]
        interval = _success_interval(states, stage_id)
        if interval is None:
            continue
        start, _ = interval
        for upstream in stage.strong_deps:
            upstream_interval = _success_interval(states, upstream)
            if upstream_interval is None:
                deviations.append(
                    _deviation(
                        stage_id,
                        "strong_dependency_order",
                        "upstream_not_succeeded",
                        {"upstream": upstream},
                    )
                )
                continue
            _, upstream_end = upstream_interval
            if upstream_end is None or start is None or upstream_end > start:
                deviations.append(
                    _deviation(
                        stage_id,
                        "strong_dependency_order",
                        "started_before_strong_upstream_finished",
                        {
                            "upstream": upstream,
                            "upstream_end_mono_ms": upstream_end,
                            "stage_start_mono_ms": start,
                        },
                    )
                )


def _check_resource_budget(dag, snapshot: Snapshot, trace: List[TraceEvent], deviations: List[dict]) -> int:
    used = 0
    peak = 0
    timeline: List[dict] = []
    for event in trace:
        if event.event == "state_change" and event.reason == "ready_to_running":
            used += event.resource_units
            peak = max(peak, used)
            timeline.append(
                {
                    "seq": event.seq,
                    "mono_ms": event.mono_ms,
                    "stage_id": event.stage_id,
                    "delta": event.resource_units,
                    "used": used,
                }
            )
            if used > snapshot.global_resource_budget:
                deviations.append(
                    _deviation(
                        event.stage_id,
                        "resource_budget",
                        "global_budget_exceeded",
                        {
                            "used": used,
                            "budget": snapshot.global_resource_budget,
                            "at_seq": event.seq,
                        },
                    )
                )
        elif event.event == "state_change" and event.reason.startswith("running_to_"):
            used -= event.resource_units
            timeline.append(
                {
                    "seq": event.seq,
                    "mono_ms": event.mono_ms,
                    "stage_id": event.stage_id,
                    "delta": -event.resource_units,
                    "used": used,
                }
            )
            if used < 0:
                deviations.append(
                    _deviation(
                        event.stage_id,
                        "resource_budget",
                        "resource_accounting_negative",
                        {"used": used, "at_seq": event.seq},
                    )
                )
    return peak


def _check_output_digests(dag, states, output_root: str, deviations: List[dict]) -> None:
    for stage_id in dag.stage_ids_sorted():
        result = states.get(stage_id)
        if result is None or result.state != "succeeded":
            continue
        path = result.output_path
        if not os.path.isabs(path):
            path = os.path.join(output_root, path)
        if not os.path.isfile(path):
            deviations.append(
                _deviation(
                    stage_id,
                    "output_digests",
                    "output_file_missing",
                    {"path": path},
                )
            )
            continue
        summary = hash_output_file(path)
        if summary.sha256 != result.output_sha256:
            deviations.append(
                _deviation(
                    stage_id,
                    "output_digests",
                    "sha256_mismatch",
                    {
                        "snapshot_sha256": result.output_sha256,
                        "actual_sha256": summary.sha256,
                    },
                )
            )
        if summary.record_count != result.output_records:
            deviations.append(
                _deviation(
                    stage_id,
                    "output_digests",
                    "record_count_mismatch",
                    {
                        "snapshot_records": result.output_records,
                        "actual_records": summary.record_count,
                    },
                )
            )


def _check_skip_basis(dag, states, deviations: List[dict]) -> None:
    for stage_id in dag.stage_ids_sorted():
        result = states.get(stage_id)
        if result is None or result.state != "skipped":
            continue
        stage = dag.stages[stage_id]
        trigger = result.skipped_by
        valid = (
            trigger in stage.strong_deps
            and trigger in states
            and states[trigger].state in ("failed", "skipped")
        )
        if not valid:
            deviations.append(
                _deviation(
                    stage_id,
                    "skip_basis",
                    "skipped_without_failed_strong_upstream",
                    {"skipped_by": trigger},
                )
            )


def _check_weak_annotations(dag, states, deviations: List[dict]) -> None:
    for stage_id in dag.stage_ids_sorted():
        result = states.get(stage_id)
        if result is None or result.state != "succeeded":
            continue
        stage = dag.stages[stage_id]
        expected = sorted(
            upstream
            for upstream in stage.weak_deps
            if upstream in states and states[upstream].state in ("failed", "skipped")
        )
        actual = sorted(result.missing_upstreams)
        if actual != expected:
            deviations.append(
                _deviation(
                    stage_id,
                    "weak_upstream_annotation",
                    "missing_upstreams_annotation_mismatch",
                    {"expected": expected, "actual": actual},
                )
            )


def _check_terminal_coverage(dag, states, deviations: List[dict]) -> None:
    for stage_id in dag.stage_ids_sorted():
        result = states.get(stage_id)
        if result is None or result.state not in ("succeeded", "failed", "skipped"):
            deviations.append(
                _deviation(
                    stage_id,
                    "terminal_coverage",
                    "stage_not_in_terminal_state",
                    {"state": result.state if result else "missing"},
                )
            )


def verify_or_raise(snapshot: Snapshot, output_root: str) -> VerificationReport:
    report = verify_snapshot(snapshot, output_root)
    if not report.ok:
        raise VerifyError(
            f"verification failed with {len(report.deviations)} deviation(s)",
            [report.to_dict()],
        )
    return report
