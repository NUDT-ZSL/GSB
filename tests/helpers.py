from __future__ import annotations

from pathlib import Path
from typing import Any

from smuggle_detector.checkpoint import CheckpointManager
from smuggle_detector.engine import run_detection_batch
from smuggle_detector.parsing import (
    compute_input_digest,
    compute_plan_digest,
    parse_sample_dir,
    plan_with_overrides,
)
from smuggle_detector.report import build_report
from smuggle_detector.rules import build_rule_catalog


def manager_for(workspace: Path, sample: Any, plan: Any) -> CheckpointManager:
    catalog = build_rule_catalog(sample.rules)
    return CheckpointManager(
        workspace,
        input_digest=compute_input_digest(sample),
        plan_digest=compute_plan_digest(plan),
        catalog_fingerprint=catalog.fingerprint,
    )


def run_sample(
    sample_dir: Path,
    workspace: Path,
    *,
    overrides: dict[str, Any] | None = None,
    resume: bool = False,
) -> tuple[dict, Any, Any, CheckpointManager]:
    sample = parse_sample_dir(sample_dir)
    assert sample.plan is not None
    plan = plan_with_overrides(sample.plan, **(overrides or {}))
    manager = manager_for(workspace, sample, plan)
    outcome = run_detection_batch(sample, plan, manager, resume=resume)
    report = build_report(
        sample=sample,
        plan=plan,
        input_digest=compute_input_digest(sample),
        plan_digest=compute_plan_digest(plan),
        catalog=outcome["catalog"],
        connections=outcome["connections"],
        summary=outcome["summary"],
        scheduling=outcome["scheduling"],
        checkpoint=outcome["checkpoint"],
    )
    return report, sample, plan, manager


def conclusion_projection(report: dict) -> dict:
    projected = {}
    for connection in report["connections"]:
        hits = sorted(
            (
                hit["code"],
                hit["direction"],
                hit["byte_offset"],
                tuple(hit["range"]),
                hit.get("sync_id"),
                hit["rule_name"],
                hit["rule_generation"],
                hit["rule_effective_ms"],
            )
            for hit in connection.get("hits", [])
        )
        errors = sorted(
            (
                error["code"],
                error.get("direction"),
                tuple(error.get("missing_ranges", [])),
                error.get("observed_connection_id"),
            )
            for error in connection.get("errors", [])
        )
        messages = sorted(
            (
                message["direction"],
                message.get("sync_id"),
                message["start_offset"],
                message["end_offset"],
                message["complete"],
                message["framing"],
            )
            for message in connection.get("messages", [])
        )
        projected[connection["connection_id"]] = {
            "status": connection["status"],
            "generation": connection.get("rule_view", {}).get("generation"),
            "hits": hits,
            "errors": errors,
            "messages": messages,
            "duplicate_count": connection.get("duplicate_count", 0),
        }
    return projected

