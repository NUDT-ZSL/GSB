"""Deterministic, reproducible report assembly (requirement 5).

All collections are normalized by stable keys before serialization, so the
same input produces byte-identical conclusions at any concurrency level.
The scheduling summary is derived from the task detail list, which keeps
summary and details cross-consistent by construction.
"""

from __future__ import annotations

from typing import Any

from . import errors
from .detector import merge_group_findings
from .models import Rejection, TaskOutcome

REPORT_VERSION = 1


def build_summary(outcomes: list[TaskOutcome]) -> dict[str, int]:
    return {
        "total": len(outcomes),
        "succeeded": sum(1 for o in outcomes if o.status == "succeeded"),
        "resource_limited": sum(1 for o in outcomes
                                if o.status in ("timeout", "memory_exceeded")),
        "failed": sum(1 for o in outcomes if o.status == "failed"),
        "rejected_queue": sum(1 for o in outcomes if o.status == "rejected_queue"),
        "not_run": sum(1 for o in outcomes if o.status == "not_run"),
    }


def build_report(*, batch_id: str, policy_fingerprint: str,
                 config_snapshot: dict[str, Any],
                 rejections: list[Rejection],
                 outcomes: list[TaskOutcome],
                 cycles: list[list[str]],
                 events: list[dict[str, str]],
                 conclusion: str,
                 budget_consumed_bytes: int) -> dict[str, Any]:
    ordered = sorted(outcomes, key=lambda o: o.task_id)
    group_findings = merge_group_findings(
        o.findings for o in ordered if o.findings is not None
    )
    cycle_findings = [
        {"code": errors.E_DEPENDENCY_CYCLE, "cycle": cycle} for cycle in cycles
    ]
    return {
        "report_version": REPORT_VERSION,
        "batch_id": batch_id,
        "policy_fingerprint": policy_fingerprint,
        "config": config_snapshot,
        "rejections": [r.to_dict() for r in
                       sorted(rejections, key=lambda r: r.index)],
        "findings": {
            "conflicts": group_findings["conflicts"],
            "shadows": group_findings["shadows"],
            "dependency_cycles": cycle_findings,
        },
        "tasks": [o.to_dict() for o in ordered],
        "events": list(events),
        "summary": build_summary(ordered),
        "batch_conclusion": conclusion,
        "budget_consumed_bytes": budget_consumed_bytes,
    }


def exit_code_for(report: dict[str, Any]) -> int:
    code = 0
    if report["rejections"]:
        code |= 1  # EXIT_REJECTIONS
    findings = report["findings"]
    if findings["conflicts"] or findings["shadows"] or findings["dependency_cycles"]:
        code |= 2  # EXIT_FINDINGS
    summary = report["summary"]
    if (summary["resource_limited"] or summary["rejected_queue"]
            or summary["not_run"] or report["batch_conclusion"] != "completed"):
        code |= 4  # EXIT_RESOURCE_EVENTS
    return code
