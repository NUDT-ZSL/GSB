"""Stable, Tkinter-independent row builders for the diagnostic UI."""

from __future__ import annotations

from typing import Any

from .analysis import AnalysisResult


def build_diagnostic_rows(
    result: AnalysisResult,
    grid: dict[str, Any] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    replicas: list[dict[str, Any]] = []
    status_by_block = {
        block_id: {
            "truth_digest": decision.truth_digest,
            "truth_nodes": set(decision.truth_nodes),
            "mismatch_nodes": set(decision.mismatch_nodes),
        }
        for block_id, decision in result.consensus.decisions.items()
    }
    for state in sorted(
        result.consensus.replicas, key=lambda item: (item.node_id, item.block_id)
    ):
        block_status = status_by_block[state.block_id]
        if state.node_id in block_status["mismatch_nodes"]:
            role = "MISMATCH"
        elif state.node_id in block_status["truth_nodes"]:
            role = "TRUTH"
        else:
            role = "OTHER"
        replicas.append(
            {
                "node_id": state.node_id,
                "block_id": state.block_id,
                "role": role,
                "byte_size": state.byte_size,
                "digest": state.digest,
                "truth_digest": block_status["truth_digest"],
            }
        )
    truth_rows: list[dict[str, Any]] = []
    for block_id in sorted(result.consensus.decisions):
        decision = result.consensus.decisions[block_id]
        truth_rows.append(
            {
                "block_id": block_id,
                "replica_count": decision.replica_count,
                "truth_digest": decision.truth_digest,
                "truth_nodes": ",".join(decision.truth_nodes),
                "mismatch_nodes": ",".join(decision.mismatch_nodes) or "-",
                "strict_majority": "yes" if decision.strict_majority else "no",
                "ratio_fixed": str(decision.ratio.ratio.value),
                "scale": str(decision.ratio.scale),
                "gap_units": str(decision.ratio.gap_units),
                "ratio_error_bound_units": str(decision.ratio.gap_error_bound_units),
                "alert_certainty": decision.ratio.certainty,
            }
        )
    task_rows: list[dict[str, Any]] = []
    for task in sorted(result.plan.tasks, key=lambda item: (item.target_node_id, item.block_id)):
        task_rows.append(
            {
                "task_id": task.task_id,
                "block_id": task.block_id,
                "source_node_id": task.source_node_id,
                "target_node_id": task.target_node_id,
                "repair_amount_fixed": str(task.repair_amount_fixed),
                "repair_error_bound_units": str(task.repair_amount_error_bound_units),
                "workload_fixed": str(task.workload.value),
                "workload_error_bound_units": str(task.workload.error_bound_units),
                "cost_fixed": str(task.cost_fixed),
                "cost_error_bound_units": str(task.cost_error_bound_units),
                "tie_channel": "yes" if task.selection.tie_channel else "no",
                "source_order": " > ".join(
                    item.candidate.node_id for item in task.selection.ranked
                ),
            }
        )
    flip_rows: list[dict[str, Any]] = []
    if grid is not None:
        for event in sorted(
            grid.get("flips", []),
            key=lambda item: (
                item["kind"],
                item["block_id"],
                item["target_node_id"],
                item["before_scale"],
                item["after_scale"],
            ),
        ):
            flip_rows.append(
                {
                    "kind": event["kind"],
                    "block_id": event["block_id"],
                    "target_node_id": event["target_node_id"],
                    "before_scale": str(event["before_scale"]),
                    "after_scale": str(event["after_scale"]),
                    "before_decision": str(event["before_decision"]),
                    "after_decision": str(event["after_decision"]),
                    "error_bound_units": str(event["error_bound_units"]),
                    "error_bound_share_ppm": str(event["error_bound_share_ppm"]),
                }
            )
    return {
        "replicas": replicas,
        "truth": truth_rows,
        "tasks": task_rows,
        "flips": flip_rows,
    }
