"""Stable report assembly and exit-code policy."""

from __future__ import annotations

from typing import Any

from .constants import REPORT_SCHEMA_VERSION, TOOL_NAME, TOOL_VERSION
from .models import Plan, Rejection, SampleInput
from .rules import RuleCatalog


def build_report(
    *,
    sample: SampleInput,
    plan: Plan,
    input_digest: str,
    plan_digest: str,
    catalog: RuleCatalog,
    connections: list[dict],
    summary: dict[str, Any],
    scheduling: dict[str, Any],
    checkpoint: dict[str, Any],
) -> dict[str, Any]:
    rejections: list[Rejection] = sorted(
        sample.rejections,
        key=lambda item: (
            item.source_category,
            item.record_index,
            item.code,
            item.detail,
        ),
    )
    ok = (
        summary["success_count"] == summary["task_count"]
        and not rejections
    )
    batch_rejections = []
    for connection in connections:
        if connection["status"] == "ok":
            continue
        codes = sorted(
            {
                error["code"]
                for error in connection.get("errors", [])
                if error["code"]
                in {
                    "E_TIMEOUT",
                    "E_QUEUE_FULL",
                    "E_BUDGET_STOPPED",
                    "E_INTERNAL",
                }
            }
        )
        batch_rejections.append(
            {
                "connection_id": connection["connection_id"],
                "status": connection["status"],
                "codes": codes,
            }
        )
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "tool": {"name": TOOL_NAME, "version": TOOL_VERSION},
        "ok": ok,
        "input_digest": input_digest,
        "plan_digest": plan_digest,
        "plan": plan.to_doc(),
        "rule_catalog": catalog.to_doc(),
        "parse_rejections": [item.to_doc() for item in rejections],
        "batch_rejections": batch_rejections,
        "connections": connections,
        "summary": summary,
        "scheduling": scheduling,
        "checkpoint": checkpoint,
    }


def exit_code_for_report(report: dict[str, Any]) -> int:
    return 0 if report.get("ok") else 1

