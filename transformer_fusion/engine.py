"""Execution engine: parse, plan, fuse, adjudicate, cache and snapshot."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import numpy as np

from .cache import (
    CacheStore,
    fused_cache_key,
    precision_tier,
)
from .errors import (
    Diagnostic,
    E_AXIS_INVALID,
    E_CACHE_CORRUPT,
    E_EPS_INVALID,
    E_INCONSISTENT,
    E_INPUT_MISSING,
    E_INPUT_SHAPE,
    E_MANIFEST_INVALID,
    E_NON_FINITE_INPUT,
    E_RESOURCE_LIMIT,
    E_SNAPSHOT_CORRUPT,
    E_TOLERANCE_CONFIG,
    E_TOLERANCE_EXCEEDED,
)
from .kernel import (
    execute_chain_only,
    execute_fused,
    execute_separate,
    output_digest,
)
from .manifest import find_fusion_pair, load_tasks, parse_case
from .model import (
    CaseOutcome,
    CasePlan,
    ExecutionReport,
    FusionPair,
    ResourceLimits,
    TaskCase,
)
from .planning import build_case_plan
from .snapshot import SnapshotStore
from .util import (
    content_hash_array,
    jsonable,
    preflight_directory,
    sha256_json,
)


DEFAULT_TOLERANCES = {
    "float16": (5e-3, 5e-2),
    "float32": (1e-6, 1e-5),
    "float64": (1e-12, 1e-10),
}


@dataclass(frozen=True)
class Tolerance:
    abs_tol: float
    rel_tol: float

    def to_dict(self) -> dict[str, float]:
        return {"abs_tol": self.abs_tol, "rel_tol": self.rel_tol}


def _resolve_tolerance(
    task: TaskCase,
    *,
    effective_dtype: str,
    abs_tol: float | None,
    rel_tol: float | None,
    overrides: Mapping[str, Mapping[str, Any]] | None,
) -> tuple[Tolerance | None, Diagnostic | None]:
    default_abs, default_rel = DEFAULT_TOLERANCES[effective_dtype]
    values = {"abs_tol": default_abs, "rel_tol": default_rel}
    if task.tolerance:
        values.update({k: float(v) for k, v in task.tolerance.items() if k in values})
    if abs_tol is not None:
        values["abs_tol"] = float(abs_tol)
    if rel_tol is not None:
        values["rel_tol"] = float(rel_tol)
    if overrides and task.case_id in overrides:
        values.update(
            {
                k: float(v)
                for k, v in overrides[task.case_id].items()
                if k in {"abs_tol", "rel_tol"}
            }
        )
    invalid = [
        name
        for name, value in values.items()
        if not np.isfinite(value) or value <= 0.0
    ]
    if invalid:
        return None, Diagnostic(
            code=E_TOLERANCE_CONFIG,
            case_id=task.case_id,
            stage="adjudication",
            message="tolerance values must be finite and strictly positive",
            details={"tolerance": values, "invalid_fields": invalid},
        )
    return Tolerance(values["abs_tol"], values["rel_tol"]), None


def _input_candidates(task: TaskCase, data_dir: Path) -> list[tuple[Path, str | None]]:
    candidates: list[tuple[Path, str | None]] = []
    if task.input_file:
        raw = Path(task.input_file)
        candidates.append((raw if raw.is_absolute() else data_dir / raw, "input"))
    candidates.append((data_dir / "cases" / f"{task.case_id}.npz", "input"))
    candidates.append((data_dir / "inputs.npz", task.case_id))
    return candidates


def load_case_input(
    task: TaskCase,
    *,
    data_dir: str | Path,
    input_arrays: Mapping[str, np.ndarray] | None,
    input_overrides: Mapping[str, np.ndarray] | None,
) -> tuple[np.ndarray | None, Diagnostic | None]:
    array: np.ndarray | None = None
    if input_overrides and task.case_id in input_overrides:
        array = np.asarray(input_overrides[task.case_id])
    elif input_arrays and task.case_id in input_arrays:
        array = np.asarray(input_arrays[task.case_id])
    else:
        base = Path(data_dir)
        errors: list[str] = []
        for path, preferred_key in _input_candidates(task, base):
            if not path.exists():
                continue
            try:
                with np.load(path, allow_pickle=False) as payload:
                    keys = list(payload.files)
                    key: str | None = preferred_key
                    if key not in keys and task.case_id in keys:
                        key = task.case_id
                    if key is None and len(keys) == 1:
                        key = keys[0]
                    if key is not None and key in keys:
                        array = np.asarray(payload[key])
                        break
                    errors.append(f"{path}: key {preferred_key!r}/{task.case_id!r} not found")
            except (OSError, ValueError) as exc:
                errors.append(f"{path}: {exc}")
        if array is None:
            return None, Diagnostic(
                code=E_INPUT_MISSING,
                case_id=task.case_id,
                stage="input",
                message="input tensor could not be loaded",
                details={"data_dir": str(data_dir), "errors": errors},
            )
    if tuple(array.shape) != tuple(task.input_shape):
        return None, Diagnostic(
            code=E_INPUT_SHAPE,
            case_id=task.case_id,
            stage="input",
            message="input tensor shape does not match manifest",
            details={"actual_shape": list(array.shape), "expected_shape": list(task.input_shape)},
        )
    return np.ascontiguousarray(array), None


def _block_for_output_index(
    coordinate: Sequence[int],
    task: TaskCase,
    block_size: int,
) -> int | None:
    try:
        inverse = tuple(int(x) for x in np.argsort(task.axis_order))
        input_coordinate = tuple(int(coordinate[inverse[dim]]) for dim in range(len(inverse)))
        flat = int(np.ravel_multi_index(input_coordinate, task.input_shape))
        return flat // max(1, block_size)
    except (ValueError, IndexError):
        return None


def _deviation_spectrum(abs_diff: np.ndarray) -> dict[str, Any]:
    edges = [0.0, 1e-15, 1e-12, 1e-9, 1e-6, 1e-3, 1.0, math.inf]
    counts, _ = np.histogram(abs_diff, bins=edges)
    labels = [
        "[0,1e-15)",
        "[1e-15,1e-12)",
        "[1e-12,1e-9)",
        "[1e-9,1e-6)",
        "[1e-6,1e-3)",
        "[1e-3,1)",
        "[1,inf)",
    ]
    return {
        "bins": labels,
        "counts": [int(x) for x in counts],
        "max_abs": float(np.max(abs_diff)) if abs_diff.size else 0.0,
        "mean_abs": float(np.mean(abs_diff)) if abs_diff.size else 0.0,
    }


def adjudicate(
    *,
    task: TaskCase,
    actual: np.ndarray,
    reference: np.ndarray,
    tolerance: Tolerance,
    effective_dtype: str,
    effective_block_size: int,
    pair: FusionPair | None,
    fused_vs_separate_byte_identical: bool | None,
) -> dict[str, Any]:
    if actual.shape != reference.shape:
        return {
            "ok": False,
            "shape_mismatch": True,
            "actual_shape": list(actual.shape),
            "reference_shape": list(reference.shape),
            "max_abs_deviation": None,
            "max_rel_deviation": None,
            "violations": [],
            "deviation_spectrum": {},
        }
    actual64 = np.asarray(actual, dtype=np.float64)
    reference64 = np.asarray(reference, dtype=np.float64)
    abs_diff = np.abs(actual64 - reference64)
    denominator = np.maximum(np.abs(reference64), 1e-300)
    rel_diff = np.where(abs_diff == 0.0, 0.0, abs_diff / denominator)
    threshold = tolerance.abs_tol + tolerance.rel_tol * np.abs(reference64)
    violation_mask = ~np.isfinite(actual64) & np.isfinite(reference64)
    violation_mask |= ~np.isfinite(reference64) & np.isfinite(actual64)
    violation_mask |= abs_diff > threshold
    positions = np.argwhere(violation_mask)
    violations: list[dict[str, Any]] = []
    source_counts: dict[str, int] = {}
    for position in positions[:50]:
        coordinate = tuple(int(x) for x in position)
        flat_index = int(np.ravel_multi_index(coordinate, actual64.shape))
        block_index = _block_for_output_index(coordinate, task, effective_block_size)
        if fused_vs_separate_byte_identical is False:
            # A mismatch between the two implementations, rather than against
            # the float64 reference, is a kernel/mapping defect.
            near_boundary = (
                block_index is not None
                and block_index * effective_block_size in {max(0, flat_index - 1), flat_index}
            )
            source = "block_boundary_write" if near_boundary else "axis_reorder_mapping"
        elif effective_dtype != "float64":
            source = "activation_precision_cast"
        else:
            source = "activation_semantics"
        source_counts[source] = source_counts.get(source, 0) + 1
        violations.append(
            {
                "flat_index": flat_index,
                "output_index": list(coordinate),
                "input_block_index": block_index,
                "reference_float64": float(reference64[coordinate]),
                "actual": float(actual64[coordinate]),
                "abs_deviation": float(abs_diff[coordinate]),
                "rel_deviation": float(rel_diff[coordinate]),
                "tolerance": float(threshold[coordinate]),
                "likely_source": source,
            }
        )
    max_abs = float(np.max(abs_diff)) if abs_diff.size else 0.0
    max_rel = float(np.max(rel_diff)) if rel_diff.size else 0.0
    nonfinite_actual = int(np.count_nonzero(~np.isfinite(actual64)))
    nonfinite_reference = int(np.count_nonzero(~np.isfinite(reference64)))
    return {
        "ok": not bool(np.any(violation_mask)),
        "abs_tol": tolerance.abs_tol,
        "rel_tol": tolerance.rel_tol,
        "max_abs_deviation": max_abs,
        "max_rel_deviation": max_rel,
        "violation_count": int(np.count_nonzero(violation_mask)),
        "violations": violations,
        "source_counts": source_counts,
        "deviation_spectrum": _deviation_spectrum(abs_diff),
        "nonfinite_actual": nonfinite_actual,
        "nonfinite_reference": nonfinite_reference,
        "major_source": max(source_counts, key=source_counts.get) if source_counts else None,
        "fused_vs_separate_byte_identical": fused_vs_separate_byte_identical,
        "dtype_used_for_actual": effective_dtype,
        "reference_dtype": "float64",
    }


def _empty_outcome(
    case_id: str,
    *,
    parsed: Any = None,
    pair: FusionPair | None = None,
    plan: CasePlan | None = None,
) -> CaseOutcome:
    return CaseOutcome(
        case_id=case_id,
        status="error",
        parsed=parsed,
        pair=pair,
        plan=plan,
    )


def _warning_diag(code: str, case_id: str, stage: str, message: str, details: Any = None) -> Diagnostic:
    return Diagnostic(
        code=code,
        case_id=case_id,
        stage=stage,
        message=message,
        severity="warning",
        details=dict(details or {}),
    )


def execute(
    manifest: str | Path | Sequence[TaskCase | Mapping[str, Any]] | None = None,
    *,
    data_dir: str | Path = "data",
    input_arrays: Mapping[str, np.ndarray] | None = None,
    input_overrides: Mapping[str, np.ndarray] | None = None,
    cache_dir: str | Path = "cache",
    snapshot_dir: str | Path = "snapshots",
    limits: ResourceLimits | Mapping[str, Any] | None = None,
    abs_tol: float | None = None,
    rel_tol: float | None = None,
    tolerance_overrides: Mapping[str, Mapping[str, Any]] | None = None,
    use_cache: bool = True,
    validate_snapshots: bool = True,
    repair_corrupt_snapshots: bool = False,
    case_ids: Sequence[str] | None = None,
    run_selfcheck: bool = True,
) -> ExecutionReport:
    """Execute a deterministic task manifest.

    The function always computes the per-operator float64 reference for every
    executable case.  A fused candidate is computed when selected by the plan
    (or for policy evidence when memory permits), then adjudicated elementwise.
    """

    resource_limits = ResourceLimits.from_mapping(limits)
    tasks = load_tasks(manifest)
    if case_ids is not None:
        selected = set(case_ids)
        tasks = [task for task in tasks if task.case_id in selected]
    report = ExecutionReport(
        manifest=str(manifest) if isinstance(manifest, (str, Path)) else None,
        resource={
            "limits": resource_limits.to_dict(),
            "batch_case_count": len(tasks),
            "execution_waves": max(
                1, math.ceil(max(1, len(tasks)) / max(1, resource_limits.max_parallel_cases))
            ),
            "serial_execution": True,
        },
    )
    report.extra["use_cache"] = bool(use_cache)
    report.extra["cache_dir"] = str(cache_dir)
    report.extra["snapshot_dir"] = str(snapshot_dir)
    seen: set[str] = set()
    for task in tasks:
        if task.case_id in seen:
            report.diagnostics.append(
                Diagnostic(
                    code=E_MANIFEST_INVALID,
                    case_id=task.case_id,
                    stage="parse",
                    message="duplicate case_id",
                )
            )
        seen.add(task.case_id)

    preflight_errors: list[Diagnostic] = []
    for path, stage in ((cache_dir, "cache_workspace"), (snapshot_dir, "snapshot_workspace")):
        preflight = preflight_directory(path, stage=stage)
        if preflight is not None:
            preflight_errors.append(preflight)
    if preflight_errors:
        report.diagnostics.extend(preflight_errors)
        return report

    cache = CacheStore(cache_dir)
    snapshots = SnapshotStore(snapshot_dir)
    stage_sets: dict[str, set[str]] = {
        "manifest": set(),
        "parse": set(),
        "feasibility": set(),
        "fusion_kernel": set(),
        "adjudication": set(),
        "cache": set(),
        "snapshot": set(),
    }

    for task in tasks:
        stage_sets["manifest"].add(task.case_id)
        outcome = _empty_outcome(task.case_id)

        # Snapshot validation happens before any content work so a corrupt
        # snapshot can never be silently patched by a new computation.
        previous_snapshot = None
        if validate_snapshots:
            previous_snapshot, snapshot_diag = snapshots.validate_case(task.case_id)
            if snapshot_diag is not None:
                if repair_corrupt_snapshots and snapshot_diag.code == E_SNAPSHOT_CORRUPT:
                    outcome.diagnostics.append(
                        _warning_diag(
                            E_SNAPSHOT_CORRUPT,
                            task.case_id,
                            "snapshot",
                            "corrupt snapshot discarded for full replacement",
                            snapshot_diag.details,
                        )
                    )
                    previous_snapshot = None
                else:
                    outcome.diagnostics.append(snapshot_diag)
                    outcome.extra["snapshot_validation"] = "corrupt"
                    report.cases.append(outcome)
                    report.diagnostics.extend(outcome.diagnostics)
                    continue

        parsed = parse_case(task)
        outcome.parsed = parsed
        stage_sets["parse"].add(task.case_id)
        if not parsed.valid:
            outcome.diagnostics.extend(parsed.diagnostics)
            report.cases.append(outcome)
            report.diagnostics.extend(parsed.diagnostics)
            continue

        pair_builder, pair_diags = find_fusion_pair(task)
        stage_sets["feasibility"].add(task.case_id)
        if pair_diags:
            outcome.diagnostics.extend(pair_diags)
            report.cases.append(outcome)
            report.diagnostics.extend(pair_diags)
            continue
        pair = pair_builder.freeze() if pair_builder is not None else None
        outcome.pair = pair

        plan = build_case_plan(task, pair, limits=resource_limits, batch_size=len(tasks))
        outcome.plan = plan
        outcome.diagnostics.extend(plan.diagnostics)
        if any(d.severity == "error" for d in plan.diagnostics):
            report.cases.append(outcome)
            report.diagnostics.extend(plan.diagnostics)
            continue

        array, input_diag = load_case_input(
            task,
            data_dir=data_dir,
            input_arrays=input_arrays,
            input_overrides=input_overrides,
        )
        if input_diag is not None or array is None:
            assert input_diag is not None
            outcome.diagnostics.append(input_diag)
            report.cases.append(outcome)
            report.diagnostics.extend([input_diag])
            continue
        input_hash = content_hash_array(array)
        outcome.input_sha256 = input_hash
        outcome.input_shape = tuple(array.shape)
        outcome.input_dtype = str(array.dtype)
        outcome.input_array = array

        # Non-finite values are a kernel-stage input contract violation.  The
        # injection path uses this exact check without modifying source code.
        if not np.all(np.isfinite(array)):
            diag = Diagnostic(
                code=E_NON_FINITE_INPUT,
                case_id=task.case_id,
                stage="fusion_kernel",
                message="input contains NaN or Inf",
                details={
                    "nonfinite_count": int(np.count_nonzero(~np.isfinite(array))),
                    "first_nonfinite_flat_index": (
                        int(np.flatnonzero(~np.isfinite(array.reshape(-1)))[0])
                        if np.any(~np.isfinite(array))
                        else None
                    ),
                },
            )
            outcome.diagnostics.append(diag)
            report.cases.append(outcome)
            report.diagnostics.append(diag)
            continue

        effective_dtype = plan.effective_dtype
        tolerance, tolerance_diag = _resolve_tolerance(
            task,
            effective_dtype=effective_dtype,
            abs_tol=abs_tol,
            rel_tol=rel_tol,
            overrides=tolerance_overrides,
        )
        if tolerance_diag is not None or tolerance is None:
            assert tolerance_diag is not None
            outcome.diagnostics.append(tolerance_diag)
            report.cases.append(outcome)
            report.diagnostics.append(tolerance_diag)
            continue

        fused_result = None
        fused_candidate_computed = False
        cache_info: dict[str, Any] = {
            "key": None,
            "hit": False,
            "status": "not_applicable",
        }
        cache_key: str | None = None
        if pair is not None and pair.feasible:
            cache_key = fused_cache_key(
                task,
                input_hash,
                effective_block_size=plan.effective_block_size,
                effective_dtype=effective_dtype,
                tier=plan.precision_tier,
            )
            cache_info.update(
                {
                    "key": cache_key,
                    "precision_tier": plan.precision_tier,
                    "includes": [
                        "input_tensor_content_sha256",
                        "op_chain_digest",
                        "axis_order",
                        "block_size",
                        "eps",
                        "precision_tier",
                    ],
                }
            )

        try:
            if pair is not None and pair.feasible:
                separate_result = execute_separate(
                    array,
                    task,
                    activation_index=pair.activation_index,
                    transpose_index=pair.transpose_index,
                    block_size=plan.effective_block_size,
                )
            else:
                separate_result = execute_chain_only(array, task)
        except (FloatingPointError, ValueError, OverflowError) as exc:
            diag = Diagnostic(
                code=E_NON_FINITE_INPUT,
                case_id=task.case_id,
                stage="fusion_kernel",
                message=f"separate reference kernel failed: {exc}",
            )
            outcome.diagnostics.append(diag)
            report.cases.append(outcome)
            report.diagnostics.append(diag)
            continue

        if pair is not None and pair.feasible and plan.fuse:
            hit = cache.get(cache_key) if use_cache else None
            if hit is not None and hit.diagnostic is not None:
                outcome.diagnostics.append(hit.diagnostic)
                outcome.cache = {"key": cache_key, "hit": False, "status": "corrupt"}
                report.cases.append(outcome)
                report.diagnostics.append(hit.diagnostic)
                continue
            if hit is not None and hit.hit and hit.array is not None and hit.entry is not None:
                cached = np.asarray(hit.array)
                if cached.shape != separate_result.output.shape:
                    diag = Diagnostic(
                        code=E_CACHE_CORRUPT,
                        case_id=task.case_id,
                        stage="cache",
                        message="cached artifact shape does not match current output",
                        details={
                            "cached_shape": list(cached.shape),
                            "expected_shape": list(separate_result.output.shape),
                        },
                    )
                    outcome.diagnostics.append(diag)
                    report.cases.append(outcome)
                    report.diagnostics.append(diag)
                    continue
                fused_float = np.asarray(cached, dtype=np.float64)
                cached_intermediate = hit.entry.get("metadata", {}).get("intermediate_digest")
                fused_result = type(separate_result)(
                    output=fused_float,
                    intermediate_digest=cached_intermediate,
                    block_boundaries=separate_result.block_boundaries,
                    metadata={
                        "path": "fused_cache_hit",
                        "materialized_intermediate_bytes": 0,
                        "cache_version": hit.entry.get("version"),
                    },
                )
                fused_candidate_computed = True
                cache_info.update(
                    {
                        "hit": True,
                        "status": "reused",
                        "version": hit.entry.get("version"),
                        "checksum": hit.entry.get("checksum"),
                        "artifact_sha256": hit.entry.get("artifact_sha256"),
                    }
                )
            else:
                fused_result = execute_fused(
                    array,
                    task,
                    activation_index=pair.activation_index,
                    transpose_index=pair.transpose_index,
                    block_size=plan.effective_block_size,
                )
                fused_candidate_computed = True
                cache_info.update({"hit": False, "status": "computed_pending_adjudication"})
        elif (
            pair is not None
            and pair.feasible
            and not plan.fuse
            and plan.fallback_category == "policy"
        ):
            # Policy fallbacks keep numerical evidence without committing the
            # fused product to cache.
            fused_result = execute_fused(
                array,
                task,
                activation_index=pair.activation_index,
                transpose_index=pair.transpose_index,
                block_size=plan.effective_block_size,
            )
            fused_candidate_computed = True
            cache_info["status"] = "evidence_only_policy_fallback"
        else:
            cache_info["status"] = "not_applicable"

        separate_committed = np.ascontiguousarray(separate_result.output.astype(effective_dtype))
        fused_committed = (
            np.ascontiguousarray(fused_result.output.astype(effective_dtype))
            if fused_result is not None
            else None
        )
        chosen_float = fused_result.output if (plan.fuse and fused_result is not None) else separate_result.output
        committed = np.ascontiguousarray(chosen_float.astype(effective_dtype))
        outcome.separate_output = separate_committed
        outcome.reference_output = np.ascontiguousarray(separate_result.output.astype(np.float64))
        outcome.fused_output = fused_committed
        outcome.committed_output = committed
        outcome.separate_intermediate_digest = separate_result.intermediate_digest
        outcome.intermediate_digest = (
            fused_result.intermediate_digest if fused_result is not None else None
        )
        outcome.separate_output_digest = output_digest(separate_committed, "tf-output-v1")
        outcome.fused_output_digest = (
            output_digest(fused_committed, "tf-output-v1")
            if fused_committed is not None
            else None
        )
        outcome.reference_digest = output_digest(outcome.reference_output, "tf-reference-f64-v1")
        outcome.committed_digest = output_digest(committed, "tf-committed-output-v1")
        outcome.block_boundaries = separate_result.block_boundaries
        outcome.extra["separate_kernel_metadata"] = dict(separate_result.metadata)
        if fused_result is not None:
            outcome.extra["fused_kernel_metadata"] = dict(fused_result.metadata)
        stage_sets["fusion_kernel"].add(task.case_id)

        byte_identical: bool | None = None
        if fused_committed is not None:
            byte_identical = (
                fused_committed.tobytes(order="C") == separate_committed.tobytes(order="C")
                and outcome.fused_output_digest == outcome.separate_output_digest
            )
            if outcome.intermediate_digest != outcome.separate_intermediate_digest:
                diag = Diagnostic(
                    code=E_INCONSISTENT,
                    case_id=task.case_id,
                    stage="fusion_kernel",
                    message="fused and separate activation intermediate digests differ",
                    details={
                        "fused": outcome.intermediate_digest,
                        "separate": outcome.separate_intermediate_digest,
                    },
                )
                outcome.diagnostics.append(diag)
                report.diagnostics.append(diag)
            if not byte_identical:
                diag = Diagnostic(
                    code=E_INCONSISTENT,
                    case_id=task.case_id,
                    stage="fusion_kernel",
                    message="fused and separate outputs are not byte-identical",
                    details={
                        "fused_digest": outcome.fused_output_digest,
                        "separate_digest": outcome.separate_output_digest,
                    },
                )
                outcome.diagnostics.append(diag)
                report.diagnostics.append(diag)

        equivalence = adjudicate(
            task=task,
            actual=committed,
            reference=outcome.reference_output,
            tolerance=tolerance,
            effective_dtype=effective_dtype,
            effective_block_size=plan.effective_block_size,
            pair=pair,
            fused_vs_separate_byte_identical=byte_identical,
        )
        equivalence.update(
            {
                "path": plan.path,
                "fused_intermediate_digest": outcome.intermediate_digest,
                "separate_intermediate_digest": outcome.separate_intermediate_digest,
                "intermediate_digest_equal": (
                    outcome.intermediate_digest == outcome.separate_intermediate_digest
                    if outcome.intermediate_digest is not None
                    else None
                ),
                "fused_candidate_computed": fused_candidate_computed,
                "block_count": len(outcome.block_boundaries),
            }
        )
        outcome.equivalence = equivalence
        stage_sets["adjudication"].add(task.case_id)
        stage_sets["cache"].add(task.case_id)

        if not equivalence["ok"]:
            positions = equivalence.get("violations", [])
            diag = Diagnostic(
                code=E_TOLERANCE_EXCEEDED,
                case_id=task.case_id,
                stage="adjudication",
                message="fused/separate result exceeds the declared elementwise tolerance",
                details={
                    "max_abs_deviation": equivalence.get("max_abs_deviation"),
                    "max_rel_deviation": equivalence.get("max_rel_deviation"),
                    "violation_count": equivalence.get("violation_count"),
                    "locations": positions[:10],
                    "deviation_spectrum": equivalence.get("deviation_spectrum"),
                    "major_source": equivalence.get("major_source"),
                },
            )
            outcome.diagnostics.append(diag)
            report.diagnostics.append(diag)
            outcome.cache = cache_info
            report.cases.append(outcome)
            continue

        if any(d.severity == "error" for d in outcome.diagnostics):
            outcome.cache = cache_info
            report.cases.append(outcome)
            continue

        if (
            plan.fuse
            and fused_result is not None
            and fused_committed is not None
            and not cache_info.get("hit")
            and use_cache
        ):
            try:
                committed_cache = cache.put(
                    cache_key,
                    fused_committed,
                    case_id=task.case_id,
                    node_type="fused_pair",
                    metadata={
                        "input_sha256": input_hash,
                        "op_chain": list(task.op_chain),
                        "axis_order": list(task.axis_order),
                        "block_size": plan.effective_block_size,
                        "eps_hex": float(task.eps).hex(),
                        "dtype": effective_dtype,
                        "precision_tier": plan.precision_tier,
                        "intermediate_digest": outcome.intermediate_digest,
                        "output_digest": outcome.fused_output_digest,
                    },
                )
                cache_info.update(
                    {
                        "hit": False,
                        "status": committed_cache["status"],
                        "version": committed_cache.get("version"),
                        "checksum": committed_cache.get("checksum"),
                        "artifact_sha256": committed_cache.get("artifact_sha256"),
                    }
                )
            except (OSError, ValueError) as exc:
                diag = Diagnostic(
                    code=E_CACHE_CORRUPT,
                    case_id=task.case_id,
                    stage="cache",
                    message=f"cache commit failed: {exc}",
                )
                outcome.diagnostics.append(diag)
                report.diagnostics.append(diag)
                outcome.cache = cache_info
                report.cases.append(outcome)
                continue
        outcome.cache = cache_info

        # Commit only after every numerical and structural check has passed.
        try:
            snapshot_info = snapshots.commit(
                task.case_id,
                plan=plan,
                output=committed,
                repair_corrupt_existing=repair_corrupt_snapshots,
                payload={
                    "input_sha256": input_hash,
                    "dtype": effective_dtype,
                    "cache_key": cache_key,
                    "cache_hit": bool(cache_info.get("hit")),
                    "cache_entry_checksum": cache_info.get("checksum"),
                    "intermediate_digest": outcome.intermediate_digest,
                    "separate_intermediate_digest": outcome.separate_intermediate_digest,
                    "output_digest": outcome.committed_digest,
                    "equivalence": jsonable(equivalence),
                    "previous_snapshot_version": (
                        previous_snapshot.version if previous_snapshot is not None else None
                    ),
                },
            )
            outcome.snapshot = snapshot_info.to_dict()
            stage_sets["snapshot"].add(task.case_id)
            outcome.status = "ok"
        except Exception as exc:  # SnapshotCorruption is intentionally surfaced.
            case_id = getattr(exc, "case_id", task.case_id)
            diag = Diagnostic(
                code=E_SNAPSHOT_CORRUPT,
                case_id=case_id,
                stage="snapshot",
                message=str(exc),
                details=getattr(exc, "details", {}),
            )
            outcome.diagnostics.append(diag)
            report.diagnostics.append(diag)
        report.cases.append(outcome)

    report.stage_case_sets = {key: sorted(value) for key, value in stage_sets.items()}
    report.cache_summary = cache.summary()
    report.snapshot_summary = snapshots.summary()
    if run_selfcheck:
        from .selfcheck import run_selfcheck

        report.selfcheck = run_selfcheck(report, cache_store=cache, snapshot_store=snapshots)
        if not report.selfcheck.get("ok", False):
            for diag in report.selfcheck.get("diagnostics", []):
                report.diagnostics.append(Diagnostic.from_mapping(diag))
    return report
