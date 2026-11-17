"""Fixed-point grid perturbation and decision-flip diagnostics."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .analysis import AnalysisResult, analyze_local
from .fixed import error_bound_share_ppm, validate_scale
from .planner import RepairTask
from .spec import ReplicaGroupSpec
from .util import JsonlEventLog, digest_json


@dataclass(frozen=True)
class GridRun:
    analysis: AnalysisResult

    def task_map(self) -> dict[tuple[str, str], RepairTask]:
        return {(task.block_id, task.target_node_id): task for task in self.analysis.plan.tasks}


def _stable_scale_list(scales: Iterable[int]) -> list[int]:
    result = sorted({validate_scale(scale) for scale in scales})
    if not result:
        raise ValueError("at least one grid scale is required")
    return result


def _source_candidate(task: RepairTask, node_id: str) -> dict[str, Any] | None:
    for item in task.selection.ranked:
        if item.candidate.node_id == node_id:
            return item.candidate.to_dict()
    return None


def _term_rows(candidate: dict[str, Any] | None) -> list[dict[str, Any]]:
    if candidate is None:
        return []
    terms = candidate.get("terms", [])
    return [
        {
            "name": term["name"],
            "fixed_value": term["fixed_value"],
            "weight": term["weight"],
            "weighted_value": term["weighted_value"],
            "weighted_error_bound_units": term["weighted_error_bound_units"],
        }
        for term in terms
        if isinstance(term, dict)
    ]


def diagnose_grid_local(
    spec: ReplicaGroupSpec,
    scales: Iterable[int],
    *,
    verify_manifest: bool = True,
    logger: JsonlEventLog | None = None,
) -> dict[str, Any]:
    """Re-evaluate the group at multiple grids and report explained flips.

    The exact truth digest and exact mismatch set are invariants and are checked
    explicitly.  Grid-dependent decisions are the ratio-alert certainty and the
    repair-source order after applying the error-bound tie channel.
    """

    scale_list = _stable_scale_list(scales)
    runs = [
        GridRun(
            analyze_local(
                spec,
                scale,
                logger=logger,
                verify_manifest=verify_manifest,
            )
        )
        for scale in scale_list
    ]
    flips: list[dict[str, Any]] = []
    invariant_violations: list[dict[str, Any]] = []
    baseline = runs[0]
    baseline_truth = {
        block_id: decision.truth_digest
        for block_id, decision in baseline.analysis.consensus.decisions.items()
    }
    baseline_mismatch = {
        block_id: set(decision.mismatch_nodes)
        for block_id, decision in baseline.analysis.consensus.decisions.items()
    }
    for run in runs[1:]:
        for block_id, decision in sorted(run.analysis.consensus.decisions.items()):
            if decision.truth_digest != baseline_truth.get(block_id):
                invariant_violations.append(
                    {
                        "kind": "TRUTH_CHANGED",
                        "block_id": block_id,
                        "scale": run.analysis.scale,
                        "baseline_truth_digest": baseline_truth.get(block_id),
                        "observed_truth_digest": decision.truth_digest,
                    }
                )
            if set(decision.mismatch_nodes) != baseline_mismatch.get(block_id, set()):
                invariant_violations.append(
                    {
                        "kind": "MISMATCH_SET_CHANGED",
                        "block_id": block_id,
                        "scale": run.analysis.scale,
                        "baseline_mismatch_nodes": sorted(
                            baseline_mismatch.get(block_id, set())
                        ),
                        "observed_mismatch_nodes": list(decision.mismatch_nodes),
                    }
                )
    for before, after in zip(runs, runs[1:]):
        before_scale = before.analysis.scale
        after_scale = after.analysis.scale
        for block_id in sorted(before.analysis.consensus.decisions):
            before_decision = before.analysis.consensus.decisions[block_id]
            after_decision = after.analysis.consensus.decisions[block_id]
            before_alert = before_decision.ratio
            after_alert = after_decision.ratio
            if before_alert.certainty != after_alert.certainty:
                gap_abs = max(abs(before_alert.gap_units), abs(after_alert.gap_units))
                share = error_bound_share_ppm(1, gap_abs)
                event = {
                    "event_id": "flip-" + digest_json(
                        ["ratio", block_id, before_scale, after_scale]
                    )[:20],
                    "kind": "RATIO_ALERT_CHANGED",
                    "block_id": block_id,
                    "target_node_id": "*",
                    "before_scale": before_scale,
                    "after_scale": after_scale,
                    "before_decision": {
                        "certainty": before_alert.certainty,
                        "observed_above": before_alert.observed_above,
                        "gap_units": before_alert.gap_units,
                        "ratio_fixed": before_alert.ratio.value,
                        "threshold_fixed": before_alert.threshold.value,
                        "exact_above": before_alert.exact_above,
                    },
                    "after_decision": {
                        "certainty": after_alert.certainty,
                        "observed_above": after_alert.observed_above,
                        "gap_units": after_alert.gap_units,
                        "ratio_fixed": after_alert.ratio.value,
                        "threshold_fixed": after_alert.threshold.value,
                        "exact_above": after_alert.exact_above,
                    },
                    "cost_terms": [],
                    "error_bound_units": 1,
                    "error_bound_share_ppm": share,
                    "explanation": (
                        "失配比例使用 floor 量化，比较差值的误差严格小于 1 个定点单位；"
                        f"格度从 {before_scale} 变为 {after_scale} 时，"
                        f"量化差从 {before_alert.gap_units} 变为 {after_alert.gap_units}，"
                        "告警确定性随之变化。精确真值与失配集合未改变。"
                    ),
                }
                flips.append(event)
        before_tasks = before.task_map()
        after_tasks = after.task_map()
        for key in sorted(set(before_tasks) | set(after_tasks)):
            before_task = before_tasks.get(key)
            after_task = after_tasks.get(key)
            if before_task is None or after_task is None:
                continue
            before_sources = [item.candidate.node_id for item in before_task.selection.ranked]
            after_sources = [item.candidate.node_id for item in after_task.selection.ranked]
            source_changed = before_task.source_node_id != after_task.source_node_id
            order_changed = before_sources != after_sources
            if not source_changed and not order_changed:
                continue
            before_old = _source_candidate(before_task, before_task.source_node_id)
            before_new = _source_candidate(before_task, after_task.source_node_id)
            after_old = _source_candidate(after_task, before_task.source_node_id)
            after_new = _source_candidate(after_task, after_task.source_node_id)
            before_gap = (
                abs(int(before_old["value_fixed"]) - int(before_new["value_fixed"]))
                if before_old is not None and before_new is not None
                else 0
            )
            after_gap = (
                abs(int(after_old["value_fixed"]) - int(after_new["value_fixed"]))
                if after_old is not None and after_new is not None
                else 0
            )
            before_bound = (
                int(before_old["error_bound_units"]) + int(before_new["error_bound_units"])
                if before_old is not None and before_new is not None
                else 0
            )
            after_bound = (
                int(after_old["error_bound_units"]) + int(after_new["error_bound_units"])
                if after_old is not None and after_new is not None
                else 0
            )
            share = error_bound_share_ppm(max(after_bound, before_bound), max(after_gap, before_gap))
            cost_terms = []
            for label, candidate in (
                ("before_source_at_before_scale", before_old),
                ("after_source_at_before_scale", before_new),
                ("before_source_at_after_scale", after_old),
                ("after_source_at_after_scale", after_new),
            ):
                if candidate is None:
                    continue
                cost_terms.append(
                    {
                        "label": label,
                        "node_id": candidate["node_id"],
                        "scale": before_scale if "before_scale" in label else after_scale,
                        "cost_fixed": candidate["value_fixed"],
                        "error_bound_units": candidate["error_bound_units"],
                        "terms": _term_rows(candidate),
                    }
                )
            kind = "SOURCE_SELECTION_CHANGED" if source_changed else "SOURCE_ORDER_CHANGED"
            flip = {
                "event_id": "flip-" + digest_json(
                    [kind, key[0], key[1], before_scale, after_scale]
                )[:20],
                "kind": kind,
                "block_id": key[0],
                "target_node_id": key[1],
                "before_scale": before_scale,
                "after_scale": after_scale,
                "before_decision": {
                    "source_node_id": before_task.source_node_id,
                    "order": before_sources,
                    "tie_channel": before_task.selection.tie_channel,
                    "tie_reason": before_task.selection.tie_reason,
                    "cost_fixed": before_task.cost_fixed,
                    "error_bound_units": before_task.cost_error_bound_units,
                },
                "after_decision": {
                    "source_node_id": after_task.source_node_id,
                    "order": after_sources,
                    "tie_channel": after_task.selection.tie_channel,
                    "tie_reason": after_task.selection.tie_reason,
                    "cost_fixed": after_task.cost_fixed,
                    "error_bound_units": after_task.cost_error_bound_units,
                },
                "cost_terms": cost_terms,
                "before_gap_units": before_gap,
                "after_gap_units": after_gap,
                "before_error_bound_units": before_bound,
                "after_error_bound_units": after_bound,
                "error_bound_units": max(before_bound, after_bound),
                "error_bound_share_ppm": share,
                "explanation": (
                    "候选成本使用 floor 量化并逐项加权；两个候选的差值若不超过"
                    "双方派生误差上界之和，则进入并列通道，按优先级降序、节点标识升序决胜。"
                    f"格度 {before_scale} 时差值/误差界为 {before_gap}/{before_bound}，"
                    f"格度 {after_scale} 时为 {after_gap}/{after_bound}，"
                    "因此成本排序或选中源发生变化。"
                ),
            }
            flips.append(flip)

    flips.sort(
        key=lambda item: (
            item["kind"],
            item["block_id"],
            item["target_node_id"],
            item["before_scale"],
            item["after_scale"],
        )
    )
    scale_states = []
    for run in runs:
        scale_states.append(
            {
                "scale": run.analysis.scale,
                "aggregate_mismatch_ratio": run.analysis.consensus.aggregate_mismatch_ratio.to_dict(),
                "blocks": [
                    {
                        "block_id": block_id,
                        "truth_digest": run.analysis.consensus.decisions[block_id].truth_digest,
                        "truth_nodes": list(run.analysis.consensus.decisions[block_id].truth_nodes),
                        "mismatch_nodes": list(run.analysis.consensus.decisions[block_id].mismatch_nodes),
                        "ratio_alert": run.analysis.consensus.decisions[block_id].ratio.to_dict(),
                    }
                    for block_id in sorted(run.analysis.consensus.decisions)
                ],
                "sources": [
                    {
                        "block_id": task.block_id,
                        "target_node_id": task.target_node_id,
                        "source_node_id": task.source_node_id,
                        "ranked_sources": [
                            item.candidate.node_id for item in task.selection.ranked
                        ],
                        "tie_channel": task.selection.tie_channel,
                    }
                    for task in run.analysis.plan.tasks
                ],
            }
        )
    return {
        "baseline_scale": scale_list[0],
        "scales": scale_list,
        "invariants": {
            "truth_digest_stable": not any(
                item["kind"] == "TRUTH_CHANGED" for item in invariant_violations
            ),
            "exact_mismatch_set_stable": not any(
                item["kind"] == "MISMATCH_SET_CHANGED" for item in invariant_violations
            ),
            "violations": invariant_violations,
        },
        "flip_count": len(flips),
        "flips": flips,
        "scale_states": scale_states,
        "error_model": {
            "truncation": "floor",
            "single_term_error_bound_units": 1,
            "ratio_comparison_bound_units": 1,
            "cost_tie_rule": "error-bound overlap: priority desc, node_id asc",
            "share_units": "ppm of decision gap, denominator clamped to one fixed unit",
        },
    }


diagnose_grid = diagnose_grid_local
