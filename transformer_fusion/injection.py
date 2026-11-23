"""Input/parameter-driven fault injection for all four business stages."""

from __future__ import annotations

import copy
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from .engine import execute, load_case_input
from .errors import (
    Diagnostic,
    E_AXIS_INVALID,
    E_INJECTION_INVALID,
    E_NON_FINITE_INPUT,
    E_OP_UNKNOWN,
    E_TOLERANCE_CONFIG,
    E_TOLERANCE_EXCEEDED,
)
from .manifest import load_tasks
from .model import ExecutionReport, ResourceLimits, TaskCase


STAGE_ALIASES = {
    "parse": "parse",
    "feasibility": "feasibility",
    "kernel": "fusion_kernel",
    "fusion_kernel": "fusion_kernel",
    "adjudication": "adjudication",
    "equivalence": "adjudication",
}


def _default_injections(tasks: Sequence[TaskCase]) -> list[dict[str, Any]]:
    if not tasks:
        return []
    parse_case = tasks[0]
    feasibility_case = tasks[1] if len(tasks) > 1 else tasks[0]
    kernel_case = tasks[2] if len(tasks) > 2 else tasks[0]
    float_case = next(
        (
            task
            for task in tasks
            if task.dtype != "float64" and task.activation_kind != "relu"
        ),
        next((task for task in tasks if task.dtype != "float64"), tasks[0]),
    )
    return [
        {
            "case_id": parse_case.case_id,
            "stage": "parse",
            "kind": "unknown_op",
            "params": {"op": "__injected_unknown_op__"},
        },
        {
            "case_id": feasibility_case.case_id,
            "stage": "feasibility",
            "kind": "duplicate_axis",
            "params": {},
        },
        {
            "case_id": kernel_case.case_id,
            "stage": "kernel",
            "kind": "non_finite_input",
            "params": {"flat_index": 0, "value": "nan"},
        },
        {
            "case_id": float_case.case_id,
            "stage": "adjudication",
            "kind": "non_positive_tolerance",
            "params": {"abs_tol": -1.0, "rel_tol": -1.0},
        },
        {
            "case_id": float_case.case_id,
            "stage": "adjudication",
            "kind": "tight_tolerance",
            "params": {"abs_tol": 1e-300, "rel_tol": 1e-300},
        },
    ]


def _expected_for(stage: str, kind: str) -> tuple[str, str]:
    if stage == "parse":
        if kind in {"unknown_op", "unknown_activation_kind"}:
            return E_OP_UNKNOWN, "parse"
    if stage == "feasibility":
        if kind in {"duplicate_axis", "non_permutation_axis"}:
            return E_AXIS_INVALID, "feasibility"
    if stage == "fusion_kernel":
        if kind in {"non_finite_input", "nan_input", "inf_input"}:
            return E_NON_FINITE_INPUT, "fusion_kernel"
    if stage == "adjudication":
        if kind in {"non_positive_tolerance", "invalid_tolerance"}:
            return E_TOLERANCE_CONFIG, "adjudication"
        if kind in {"tight_tolerance", "zero_tolerance"}:
            return E_TOLERANCE_EXCEEDED, "adjudication"
    return E_INJECTION_INVALID, stage


def _find_record(
    report: ExecutionReport,
    *,
    case_id: str,
    stage: str,
    code: str,
) -> dict[str, Any] | None:
    for diagnostic in report.diagnostics:
        if diagnostic.case_id == case_id and diagnostic.stage == stage and diagnostic.code == code:
            return diagnostic.to_dict()
    return None


def _modify_task(
    task: TaskCase,
    *,
    stage: str,
    kind: str,
    params: Mapping[str, Any],
) -> tuple[TaskCase, dict[str, np.ndarray] | None, Mapping[str, Mapping[str, Any]] | None]:
    input_override: dict[str, np.ndarray] | None = None
    tolerance_override: dict[str, Mapping[str, Any]] | None = None
    changed = task
    if stage == "parse":
        if kind == "unknown_op":
            changed = task.evolved(op_chain=task.op_chain + (str(params.get("op", "__injected_unknown_op__")),))
        elif kind == "unknown_activation_kind":
            changed = task.evolved(activation_kind="__injected_unknown_activation__")
        else:
            raise ValueError(f"unsupported parse injection kind: {kind}")
    elif stage == "feasibility":
        if kind in {"duplicate_axis", "non_permutation_axis"}:
            ndim = len(task.input_shape)
            if kind == "duplicate_axis" and ndim > 1:
                axis = (0, 0) + tuple(range(2, ndim))
            else:
                axis = tuple(range(ndim - 1, -1, -1)) if ndim else (0,)
                if kind == "non_permutation_axis":
                    axis = tuple([0] + list(range(1, ndim))) if ndim else (0,)
                    axis = axis[:-1] + (ndim,) if ndim else (0,)
            changed = task.evolved(axis_order=tuple(int(x) for x in axis))
        else:
            raise ValueError(f"unsupported feasibility injection kind: {kind}")
    elif stage == "fusion_kernel":
        if kind not in {"non_finite_input", "nan_input", "inf_input"}:
            raise ValueError(f"unsupported kernel injection kind: {kind}")
        # The caller replaces this placeholder after loading the source input.
    elif stage == "adjudication":
        if kind in {"non_positive_tolerance", "invalid_tolerance"}:
            tolerance_override = {
                task.case_id: {
                    "abs_tol": float(params.get("abs_tol", -1.0)),
                    "rel_tol": float(params.get("rel_tol", -1.0)),
                }
            }
        elif kind in {"tight_tolerance", "zero_tolerance"}:
            tolerance_override = {
                task.case_id: {
                    "abs_tol": float(params.get("abs_tol", 1e-300)),
                    "rel_tol": float(params.get("rel_tol", 1e-300)),
                }
            }
        else:
            raise ValueError(f"unsupported adjudication injection kind: {kind}")
    else:
        raise ValueError(f"unknown injection stage: {stage}")
    return changed, input_override, tolerance_override


def _comparison_signature(outcome: Any) -> dict[str, Any]:
    return {
        "status": outcome.status,
        "input_sha256": outcome.input_sha256,
        "committed_digest": outcome.committed_digest,
        "fused_output_digest": outcome.fused_output_digest,
        "separate_output_digest": outcome.separate_output_digest,
        "intermediate_digest": outcome.intermediate_digest,
        "cache_key": outcome.cache.get("key"),
        "plan_path": outcome.plan.path if outcome.plan else None,
        "plan_feasible": outcome.plan.feasible if outcome.plan else None,
        "fallback_category": outcome.plan.fallback_category if outcome.plan else None,
    }


def inject_diagnose(
    manifest: str | Path | Sequence[TaskCase | Mapping[str, Any]] | None = None,
    *,
    injections: Sequence[Mapping[str, Any]] | None = None,
    data_dir: str | Path = "data",
    cache_dir: str | Path = "cache",
    snapshot_dir: str | Path = "snapshots",
    input_arrays: Mapping[str, np.ndarray] | None = None,
    limits: ResourceLimits | Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run each injection in an isolated cache/snapshot namespace.

    The baseline and every injected run start from the same immutable task
    objects.  Only the target case receives a copied mutation (or an input
    override), so all other cases must retain byte-identical results and cache
    keys.
    """

    tasks = load_tasks(manifest)
    specs = [dict(item) for item in (injections or _default_injections(tasks))]
    injection_root = Path(cache_dir) / "_injections"
    snapshot_root = Path(snapshot_dir) / "_injections"
    baseline_report = execute(
        tasks,
        data_dir=data_dir,
        input_arrays=input_arrays,
        cache_dir=injection_root / "baseline" / "cache",
        snapshot_dir=snapshot_root / "baseline" / "snapshots",
        limits=limits,
        run_selfcheck=False,
    )
    baseline_map = {outcome.case_id: outcome for outcome in baseline_report.cases}
    baseline_signatures = {
        case_id: _comparison_signature(outcome) for case_id, outcome in baseline_map.items()
    }

    records: list[dict[str, Any]] = []
    all_isolated = True
    all_expected = True
    for index, spec in enumerate(specs):
        case_id = str(spec.get("case_id", ""))
        stage = STAGE_ALIASES.get(str(spec.get("stage", "")), str(spec.get("stage", "")))
        kind = str(spec.get("kind", ""))
        params = dict(spec.get("params", {}))
        expected_code, expected_stage = _expected_for(stage, kind)
        try:
            target_index = next(i for i, task in enumerate(tasks) if task.case_id == case_id)
            target = tasks[target_index]
            changed, _, tolerance_override = _modify_task(
                target, stage=stage, kind=kind, params=params
            )
            input_override: dict[str, np.ndarray] = {}
            if stage == "fusion_kernel":
                array, input_diag = load_case_input(
                    target,
                    data_dir=data_dir,
                    input_arrays=input_arrays,
                    input_overrides=None,
                )
                if input_diag is not None or array is None:
                    raise ValueError(input_diag.message if input_diag else "input missing")
                mutated = np.array(array, copy=True)
                flat = mutated.reshape(-1)
                flat_index = int(params.get("flat_index", 0))
                if flat_index < 0 or flat_index >= flat.size:
                    raise ValueError("flat_index is outside the tensor")
                marker = str(params.get("value", "nan")).lower()
                flat[flat_index] = np.inf if marker in {"inf", "+inf"} else np.nan
                input_override[case_id] = mutated
                changed = target
            run_tasks = list(tasks)
            run_tasks[target_index] = changed
            injected_report = execute(
                run_tasks,
                data_dir=data_dir,
                input_arrays=input_arrays,
                input_overrides=input_override,
                cache_dir=injection_root / f"{index:02d}_{kind}" / "cache",
                snapshot_dir=snapshot_root / f"{index:02d}_{kind}" / "snapshots",
                limits=limits,
                tolerance_overrides=tolerance_override,
                run_selfcheck=False,
            )
            record = _find_record(
                injected_report,
                case_id=case_id,
                stage=expected_stage,
                code=expected_code,
            )
            injected_map = {outcome.case_id: outcome for outcome in injected_report.cases}
            comparisons: dict[str, Any] = {}
            isolated = True
            for other_id, baseline_outcome in baseline_map.items():
                if other_id == case_id:
                    continue
                actual = injected_map.get(other_id)
                expected_signature = baseline_signatures[other_id]
                actual_signature = _comparison_signature(actual) if actual is not None else None
                same = actual_signature == expected_signature
                comparisons[other_id] = {
                    "unchanged": same,
                    "baseline": expected_signature,
                    "injected": actual_signature,
                }
                isolated = isolated and same
            expected_found = record is not None and record.get("code") == expected_code
            all_isolated = all_isolated and isolated
            all_expected = all_expected and expected_found
            records.append(
                {
                    "injection_id": f"{index:02d}_{stage}_{kind}",
                    "case_id": case_id,
                    "stage": expected_stage,
                    "kind": kind,
                    "expected_code": expected_code,
                    "business_code": record.get("code") if record else None,
                    "record": record,
                    "expected_code_observed": expected_found,
                    "unaffected_cases_unchanged": isolated,
                    "unaffected_case_count": len(comparisons),
                    "comparisons": comparisons,
                }
            )
        except Exception as exc:
            all_expected = False
            records.append(
                {
                    "injection_id": f"{index:02d}_{stage}_{kind}",
                    "case_id": case_id,
                    "stage": expected_stage,
                    "kind": kind,
                    "expected_code": expected_code,
                    "business_code": E_INJECTION_INVALID,
                    "expected_code_observed": False,
                    "unaffected_cases_unchanged": False,
                    "error": str(exc),
                }
            )
    return {
        "ok": baseline_report.ok and all_expected and all_isolated,
        "baseline_ok": baseline_report.ok,
        "baseline_summary": baseline_report.to_dict()["summary"],
        "injection_count": len(records),
        "all_expected_codes_observed": all_expected,
        "all_unaffected_cases_unchanged": all_isolated,
        "records": records,
        "stages": sorted({record["stage"] for record in records}),
        "business_codes": sorted(
            {record.get("business_code") for record in records if record.get("business_code")}
        ),
        "baseline_cache": baseline_report.cache_summary,
        "baseline_snapshot": baseline_report.snapshot_summary,
    }
