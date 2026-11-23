"""Cross-module consistency checks and the public equivalence report."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from .cache import CacheStore, fused_cache_key
from .errors import Diagnostic, E_INCONSISTENT
from .kernel import independent_fused_recompute
from .model import ExecutionReport, TaskCase
from .snapshot import SnapshotStore


def _stage_set_diagnostic(
    stage: str,
    expected: set[str],
    actual: set[str],
) -> Diagnostic | None:
    if expected == actual:
        return None
    return Diagnostic(
        code=E_INCONSISTENT,
        case_id=sorted(expected ^ actual)[0] if expected ^ actual else "*",
        stage=stage,
        message="case_id set differs from the expected processing set",
        details={
            "missing": sorted(expected - actual),
            "unexpected": sorted(actual - expected),
        },
    )


def _within_tolerance(actual: np.ndarray, expected: np.ndarray, equivalence: Mapping[str, Any]) -> bool:
    if actual.shape != expected.shape:
        return False
    if np.array_equal(actual, expected):
        return True
    abs_tol = float(equivalence.get("abs_tol", 0.0))
    rel_tol = float(equivalence.get("rel_tol", 0.0))
    if abs_tol <= 0.0 or rel_tol <= 0.0:
        return False
    diff = np.abs(actual.astype(np.float64) - expected.astype(np.float64))
    reference = np.abs(expected.astype(np.float64))
    return bool(np.all(diff <= abs_tol + rel_tol * reference))


def run_selfcheck(
    report: ExecutionReport,
    *,
    cache_store: CacheStore | None = None,
    snapshot_store: SnapshotStore | None = None,
    cache_diagnosis: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    diagnostics: list[Diagnostic] = []
    outcomes = {case.case_id: case for case in report.cases}
    manifest_set = set(report.stage_case_sets.get("manifest", []))

    # A corrupt snapshot terminates before parsing by design.  Every other
    # manifest case must be accounted for at each stage.
    snapshot_terminal = {
        case_id
        for case_id, outcome in outcomes.items()
        if any(d.stage == "snapshot" and d.severity == "error" for d in outcome.diagnostics)
    }
    parse_expected = manifest_set - snapshot_terminal
    parse_actual = set(report.stage_case_sets.get("parse", []))
    diag = _stage_set_diagnostic("parse", parse_expected, parse_actual)
    if diag:
        diagnostics.append(diag)

    feasibility_expected = {
        case_id
        for case_id, outcome in outcomes.items()
        if outcome.parsed is not None and outcome.parsed.valid
    }
    diag = _stage_set_diagnostic(
        "feasibility",
        feasibility_expected,
        set(report.stage_case_sets.get("feasibility", [])),
    )
    if diag:
        diagnostics.append(diag)

    kernel_expected = {
        case_id for case_id, outcome in outcomes.items() if outcome.separate_output is not None
    }
    diag = _stage_set_diagnostic(
        "fusion_kernel",
        kernel_expected,
        set(report.stage_case_sets.get("fusion_kernel", [])),
    )
    if diag:
        diagnostics.append(diag)

    adjudication_expected = {
        case_id for case_id, outcome in outcomes.items() if bool(outcome.equivalence)
    }
    diag = _stage_set_diagnostic(
        "adjudication",
        adjudication_expected,
        set(report.stage_case_sets.get("adjudication", [])),
    )
    if diag:
        diagnostics.append(diag)

    cache_expected = set(adjudication_expected)
    diag = _stage_set_diagnostic(
        "cache", cache_expected, set(report.stage_case_sets.get("cache", []))
    )
    if diag:
        diagnostics.append(diag)

    snapshot_expected = {
        case_id for case_id, outcome in outcomes.items() if outcome.status == "ok"
    }
    diag = _stage_set_diagnostic(
        "snapshot", snapshot_expected, set(report.stage_case_sets.get("snapshot", []))
    )
    if diag:
        diagnostics.append(diag)

    cache_checks: dict[str, Any] = {}
    equivalence_checks: dict[str, Any] = {}
    snapshot_checks: dict[str, Any] = {}
    use_cache = bool(report.extra.get("use_cache", True))

    for case_id, outcome in outcomes.items():
        task = outcome.parsed.task if outcome.parsed is not None else None
        plan = outcome.plan
        if task is not None and plan is not None and outcome.input_sha256:
            expected_key = fused_cache_key(
                task,
                outcome.input_sha256,
                effective_block_size=plan.effective_block_size,
                effective_dtype=plan.effective_dtype,
                tier=plan.precision_tier,
            )
            recorded_key = outcome.cache.get("key")
            if plan.fuse and recorded_key and recorded_key != expected_key:
                diagnostics.append(
                    Diagnostic(
                        code=E_INCONSISTENT,
                        case_id=case_id,
                        stage="cache",
                        message="recorded cache key cannot be recomputed from task/config",
                        details={"recorded": recorded_key, "recomputed": expected_key},
                    )
                )
            if (
                plan.fuse
                and outcome.status == "ok"
                and use_cache
                and cache_store is not None
                and expected_key
            ):
                lookup = cache_store.get(expected_key)
                cache_checks[case_id] = lookup.to_dict()
                if not lookup.hit:
                    diagnostics.append(
                        Diagnostic(
                            code=E_INCONSISTENT,
                            case_id=case_id,
                            stage="cache",
                            message="committed fused output has no verifiable cache artifact",
                            details={"key": expected_key, "lookup": lookup.to_dict()},
                        )
                    )
                else:
                    entry = lookup.entry or {}
                    metadata = entry.get("metadata", {})
                    if metadata.get("input_sha256") != outcome.input_sha256:
                        diagnostics.append(
                            Diagnostic(
                                code=E_INCONSISTENT,
                                case_id=case_id,
                                stage="cache",
                                message="cache metadata input hash mismatch",
                            )
                        )
                    if metadata.get("intermediate_digest") != outcome.intermediate_digest:
                        diagnostics.append(
                            Diagnostic(
                                code=E_INCONSISTENT,
                                case_id=case_id,
                                stage="cache",
                                message="cache metadata intermediate digest mismatch",
                                details={
                                    "cache": metadata.get("intermediate_digest"),
                                    "kernel": outcome.intermediate_digest,
                                },
                            )
                        )
                    if (
                        outcome.fused_output is not None
                        and lookup.array is not None
                        and not np.array_equal(
                            np.asarray(lookup.array), np.asarray(outcome.fused_output)
                        )
                    ):
                        diagnostics.append(
                            Diagnostic(
                                code=E_INCONSISTENT,
                                case_id=case_id,
                                stage="cache",
                                message="cache artifact and fused output differ",
                            )
                        )

        if outcome.status == "ok" and snapshot_store is not None:
            info, snap_diag = snapshot_store.validate_case(case_id)
            snapshot_checks[case_id] = {
                "version": info.version if info else None,
                "checksum": info.checksum if info else None,
            }
            if snap_diag is not None or info is None:
                diagnostics.append(
                    Diagnostic(
                        code=snap_diag.code if snap_diag else E_INCONSISTENT,
                        case_id=case_id,
                        stage="snapshot",
                        message=snap_diag.message if snap_diag else "committed case has no snapshot",
                        details=snap_diag.details if snap_diag else {},
                    )
                )
            elif (
                outcome.snapshot.get("version") != info.version
                or outcome.snapshot.get("checksum") != info.checksum
            ):
                diagnostics.append(
                    Diagnostic(
                        code=E_INCONSISTENT,
                        case_id=case_id,
                        stage="snapshot",
                        message="reported snapshot version/checksum differs from store",
                    )
                )

        if (
            task is not None
            and outcome.input_array is not None
            and outcome.pair is not None
            and outcome.pair.feasible
            and outcome.fused_output is not None
            and plan is not None
        ):
            independent = independent_fused_recompute(
                outcome.input_array,
                task,
                outcome.pair.activation_index,
                outcome.pair.transpose_index,
            ).astype(plan.effective_dtype)
            exact = bool(np.array_equal(independent, outcome.fused_output))
            tolerance_ok = _within_tolerance(independent, outcome.fused_output, outcome.equivalence)
            equivalence_checks[case_id] = {
                "exact": exact,
                "within_tolerance": tolerance_ok,
                "max_abs_difference": float(
                    np.max(np.abs(independent.astype(np.float64) - outcome.fused_output.astype(np.float64)))
                )
                if independent.shape == outcome.fused_output.shape and independent.size
                else None,
            }
            if not tolerance_ok:
                diagnostics.append(
                    Diagnostic(
                        code=E_INCONSISTENT,
                        case_id=case_id,
                        stage="selfcheck_equivalence",
                        message="fused output cannot be independently recomputed from activation and axis mapping",
                        details=equivalence_checks[case_id],
                    )
                )
            if (
                outcome.separate_output is not None
                and not np.array_equal(outcome.fused_output, outcome.separate_output)
            ):
                diagnostics.append(
                    Diagnostic(
                        code=E_INCONSISTENT,
                        case_id=case_id,
                        stage="selfcheck_equivalence",
                        message="fused and separate committed outputs differ byte-wise",
                    )
                )

    invalidation_check: dict[str, Any] | None = None
    if cache_diagnosis is not None:
        invalid = {
            f"{case_id}::{node}"
            for case_id, nodes in cache_diagnosis.get("invalidated_nodes", {}).items()
            for node in nodes
        }
        reused = {
            f"{case_id}::{node}"
            for case_id, nodes in cache_diagnosis.get("reused_nodes", {}).items()
            for node in nodes
        }
        disjoint = not (invalid & reused)
        covered = bool(cache_diagnosis.get("union_covers_all_affected_nodes", False))
        invalidation_check = {
            "invalid_reused_disjoint": disjoint,
            "union_covers_all": covered,
            "invalid_node_count": len(invalid),
            "reused_node_count": len(reused),
        }
        if not disjoint or not covered:
            diagnostics.append(
                Diagnostic(
                    code=E_INCONSISTENT,
                    case_id="*",
                    stage="cache_invalidation",
                    message="invalidation/reuse sets are not a disjoint covering partition",
                    details=invalidation_check,
                )
            )

    return {
        "ok": not any(d.severity == "error" for d in diagnostics),
        "case_id_sets": {
            "manifest": sorted(manifest_set),
            "parse_expected": sorted(parse_expected),
            "parse_actual": sorted(parse_actual),
            "kernel_expected": sorted(kernel_expected),
            "adjudication_expected": sorted(adjudication_expected),
            "snapshot_expected": sorted(snapshot_expected),
        },
        "cache_checks": cache_checks,
        "snapshot_checks": snapshot_checks,
        "independent_equivalence": equivalence_checks,
        "invalidation_check": invalidation_check,
        "diagnostics": [d.to_dict() for d in diagnostics],
    }


def equivalence_report(
    manifest: str | Path | Sequence[TaskCase | Mapping[str, Any]] | ExecutionReport | None = None,
    *,
    data_dir: str | Path = "data",
    cache_dir: str | Path = "cache",
    snapshot_dir: str | Path = "snapshots",
    cache_diagnosis: Mapping[str, Any] | None = None,
    **execute_kwargs: Any,
) -> dict[str, Any]:
    """Public equivalence gate.

    Accepts either an existing ExecutionReport or any manifest accepted by
    :func:`execute`.  Returns elementwise evidence plus cross-module selfcheck
    findings.
    """

    if isinstance(manifest, ExecutionReport):
        report = manifest
        if report.extra.get("cache_dir") and cache_dir == "cache":
            cache_dir = report.extra["cache_dir"]
        if report.extra.get("snapshot_dir") and snapshot_dir == "snapshots":
            snapshot_dir = report.extra["snapshot_dir"]
    else:
        from .engine import execute

        report = execute(
            manifest,
            data_dir=data_dir,
            cache_dir=cache_dir,
            snapshot_dir=snapshot_dir,
            **execute_kwargs,
        )
    cache = CacheStore(cache_dir)
    snapshots = SnapshotStore(snapshot_dir)
    selfcheck = run_selfcheck(
        report,
        cache_store=cache,
        snapshot_store=snapshots,
        cache_diagnosis=cache_diagnosis,
    )
    cases = []
    for outcome in report.cases:
        cases.append(
            {
                "case_id": outcome.case_id,
                "status": outcome.status,
                "path": outcome.plan.path if outcome.plan else None,
                "tolerance_ok": bool(outcome.equivalence.get("ok", False)),
                "byte_identical_fused_separate": outcome.equivalence.get(
                    "fused_vs_separate_byte_identical"
                ),
                "max_abs_deviation": outcome.equivalence.get("max_abs_deviation"),
                "max_rel_deviation": outcome.equivalence.get("max_rel_deviation"),
                "violation_count": outcome.equivalence.get("violation_count", 0),
                "major_source": outcome.equivalence.get("major_source"),
                "cache_key": outcome.cache.get("key"),
                "cache_hit": outcome.cache.get("hit"),
                "snapshot_version": outcome.snapshot.get("version"),
                "snapshot_checksum": outcome.snapshot.get("checksum"),
            }
        )
    return {
        "ok": report.ok and selfcheck["ok"],
        "case_count": len(cases),
        "cases": cases,
        "selfcheck": selfcheck,
        "execution_summary": report.to_dict()["summary"],
        "diagnostics": [d.to_dict() for d in report.diagnostics],
    }
