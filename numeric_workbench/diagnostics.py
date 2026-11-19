"""Chunk/full comparisons, precision spectra and recomputation checks."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

import numpy as np

from .config import PRECISIONS, TrainingConfig
from .data import Dataset, DatasetSplit
from .errors import WorkbenchError, inconsistent, tolerance_exceeded
from .features import PreparedData, prepare_dataset
from .precision import PrecisionEngine, alternate_rounding_mode, make_engine
from .serialization import (
    encode_array,
    percentile_stats,
    sha256_json,
)
from .training import TrainingRun, train_prepared

REQUIRED_STAGE_NAMES = (
    "standardization",
    "chunk_accumulation",
    "fixed_point_quantization",
    "rounding_mode",
    "learning_rate_scaling",
)


@dataclass
class DiagnosticsResult:
    payload: dict[str, Any]
    runs: dict[str, dict[str, TrainingRun]]
    prepared: dict[str, PreparedData]
    reference_full: TrainingRun
    reference_chunked: TrainingRun

    def comparison_for(self, precision: str) -> dict[str, Any]:
        return self.payload["precision_switch"]["variants"][precision]["chunk_full"]


def _selected_convergence_round(run: TrainingRun) -> int | None:
    if run.stop_reason != "converged":
        return None
    for item in run.decisions:
        if item["selected_converged"]:
            return int(item["round"])
    return None


def _relative_floor(reference_run: TrainingRun) -> float:
    values = [1.0]
    if reference_run.loss_trajectory:
        values.append(abs(reference_run.loss_trajectory[-1]))
    values.extend(abs(value) for value in reference_run.params_float)
    return max(1e-12, 64.0 * np.finfo(np.float64).eps * max(values))


def compare_runs(
    candidate: TrainingRun,
    reference: TrainingRun,
    *,
    tolerance: float,
) -> dict[str, Any]:
    """Round-by-round loss and parameter comparison in ascending round order."""

    parameter_names = reference.prepared.feature_names
    floor = _relative_floor(reference)
    common_rounds = min(candidate.rounds, reference.rounds)
    rounds: list[dict[str, Any]] = []
    first_exceedance: dict[str, Any] | None = None
    max_abs_seen = 0.0

    for index in range(common_rounds):
        round_number = index + 1
        loss_reference = float(reference.loss_trajectory[index])
        loss_candidate = float(candidate.loss_trajectory[index])
        loss_abs = abs(loss_candidate - loss_reference)
        loss_rel = loss_abs / max(abs(loss_reference), floor)
        ref_round_params = reference.prepared.engine.to_float(
            reference.parameter_trajectory[index]
        )
        cand_round_params = candidate.prepared.engine.to_float(
            candidate.parameter_trajectory[index]
        )
        parameter_rows: list[dict[str, Any]] = []
        parameter_max_abs = 0.0
        parameter_max_name = parameter_names[0]
        for parameter_index, name in enumerate(parameter_names):
            ref_value = float(ref_round_params[parameter_index])
            cand_value = float(cand_round_params[parameter_index])
            abs_deviation = abs(cand_value - ref_value)
            rel_deviation = abs_deviation / max(abs(ref_value), floor)
            parameter_rows.append(
                {
                    "name": name,
                    "reference": ref_value,
                    "candidate": cand_value,
                    "abs_deviation": abs_deviation,
                    "relative_deviation": rel_deviation,
                }
            )
            if abs_deviation > parameter_max_abs:
                parameter_max_abs = abs_deviation
                parameter_max_name = name
        round_max = max(loss_abs, parameter_max_abs)
        max_abs_seen = max(max_abs_seen, round_max)
        entry = {
            "round": round_number,
            "loss_reference": loss_reference,
            "loss_candidate": loss_candidate,
            "loss_abs_deviation": loss_abs,
            "loss_relative_deviation": loss_rel,
            "parameter_abs_deviation": parameter_rows,
            "parameter_max_abs_deviation": parameter_max_abs,
            "parameter_max_name": parameter_max_name,
            "max_abs_deviation": round_max,
            "within_tolerance": round_max <= tolerance,
        }
        rounds.append(entry)
        if first_exceedance is None and round_max > tolerance:
            if loss_abs >= parameter_max_abs:
                first_exceedance = {
                    "round": round_number,
                    "location": "loss",
                    "observed": loss_abs,
                    "tolerance": tolerance,
                }
            else:
                first_exceedance = {
                    "round": round_number,
                    "location": "parameter",
                    "parameter": parameter_max_name,
                    "observed": parameter_max_abs,
                    "tolerance": tolerance,
                }

    if first_exceedance is None and candidate.rounds != reference.rounds:
        first_exceedance = {
            "round": common_rounds + 1,
            "location": "round_count",
            "observed": abs(candidate.rounds - reference.rounds),
            "tolerance": 0.0,
        }

    final_parameter_rows: list[dict[str, Any]] = []
    ref_params = reference.prepared.engine.to_float(reference.params)
    cand_params = candidate.prepared.engine.to_float(candidate.params)
    for parameter_index, name in enumerate(parameter_names):
        ref_value = float(ref_params[parameter_index])
        cand_value = float(cand_params[parameter_index])
        abs_deviation = abs(cand_value - ref_value)
        final_parameter_rows.append(
            {
                "name": name,
                "reference": ref_value,
                "candidate": cand_value,
                "abs_deviation": abs_deviation,
                "relative_deviation": abs_deviation / max(abs(ref_value), floor),
            }
        )

    final_loss_abs = (
        abs(candidate.loss_trajectory[-1] - reference.loss_trajectory[-1])
        if candidate.loss_trajectory and reference.loss_trajectory
        else 0.0
    )
    final_parameter_max = max(
        (item["abs_deviation"] for item in final_parameter_rows), default=0.0
    )
    if first_exceedance is None and final_parameter_max > tolerance:
        worst = max(final_parameter_rows, key=lambda item: item["abs_deviation"])
        first_exceedance = {
            "round": "final",
            "location": "parameter",
            "parameter": worst["name"],
            "observed": final_parameter_max,
            "tolerance": tolerance,
        }

    reference_convergence = _selected_convergence_round(reference)
    candidate_convergence = _selected_convergence_round(candidate)
    convergence_match = reference_convergence == candidate_convergence
    if first_exceedance is None and not convergence_match:
        first_exceedance = {
            "round": candidate_convergence
            if candidate_convergence is not None
            else reference_convergence,
            "location": "convergence_round",
            "reference": reference_convergence,
            "candidate": candidate_convergence,
            "tolerance": tolerance,
        }

    return {
        "candidate_precision": candidate.precision,
        "candidate_reduce_mode": candidate.reduce_mode,
        "reference_precision": reference.precision,
        "reference_reduce_mode": reference.reduce_mode,
        "tolerance": tolerance,
        "rounds_compared": common_rounds,
        "round_count_match": candidate.rounds == reference.rounds,
        "candidate_rounds": candidate.rounds,
        "reference_rounds": reference.rounds,
        "rounds": rounds,
        "final_loss_abs_deviation": final_loss_abs,
        "final_parameter_max_abs_deviation": final_parameter_max,
        "final_parameter_spectrum": final_parameter_rows,
        "max_round_abs_deviation": max_abs_seen,
        "convergence_rounds_equal": convergence_match,
        "reference_convergence_round": reference_convergence,
        "candidate_convergence_round": candidate_convergence,
        "within_tolerance": first_exceedance is None,
        "first_exceedance": first_exceedance,
    }


def deviation_metrics(
    reference: TrainingRun,
    candidate: TrainingRun,
) -> dict[str, Any]:
    floor = _relative_floor(reference)
    common = min(reference.rounds, candidate.rounds)
    loss_abs: list[float] = []
    loss_rel: list[float] = []
    round_parameters: list[dict[str, Any]] = []
    for index in range(common):
        ref_loss = float(reference.loss_trajectory[index])
        cand_loss = float(candidate.loss_trajectory[index])
        absolute = abs(cand_loss - ref_loss)
        loss_abs.append(absolute)
        loss_rel.append(absolute / max(abs(ref_loss), floor))
        ref_params = reference.prepared.engine.to_float(
            reference.parameter_trajectory[index]
        )
        cand_params = candidate.prepared.engine.to_float(
            candidate.parameter_trajectory[index]
        )
        parameter_rows = []
        for parameter_index, name in enumerate(reference.prepared.feature_names):
            ref_value = float(ref_params[parameter_index])
            cand_value = float(cand_params[parameter_index])
            parameter_rows.append(
                {
                    "name": name,
                    "reference": ref_value,
                    "candidate": cand_value,
                    "abs_deviation": abs(cand_value - ref_value),
                    "relative_deviation": abs(cand_value - ref_value)
                    / max(abs(ref_value), floor),
                }
            )
        round_parameters.append(
            {
                "round": index + 1,
                "loss_reference": ref_loss,
                "loss_candidate": cand_loss,
                "loss_abs_deviation": absolute,
                "loss_relative_deviation": loss_rel[-1],
                "parameter_abs_deviation": parameter_rows,
                "parameter_max_abs_deviation": max(
                    (item["abs_deviation"] for item in parameter_rows), default=0.0
                ),
            }
        )

    ref_params = reference.prepared.engine.to_float(reference.params)
    cand_params = candidate.prepared.engine.to_float(candidate.params)
    final_parameters = []
    for parameter_index, name in enumerate(reference.prepared.feature_names):
        ref_value = float(ref_params[parameter_index])
        cand_value = float(cand_params[parameter_index])
        absolute = abs(cand_value - ref_value)
        final_parameters.append(
            {
                "name": name,
                "reference": ref_value,
                "candidate": cand_value,
                "abs_deviation": absolute,
                "relative_deviation": absolute / max(abs(ref_value), floor),
            }
        )
    final_param_abs = [item["abs_deviation"] for item in final_parameters]
    final_param_rel = [item["relative_deviation"] for item in final_parameters]
    all_abs = loss_abs + final_param_abs
    all_rel = loss_rel + final_param_rel
    max_abs = max(all_abs, default=0.0)
    noise_floor = max(
        64.0 * np.finfo(np.float64).eps * max(1.0, abs(reference.loss_trajectory[-1]) if reference.loss_trajectory else 1.0),
        1e-300,
    )
    return {
        "reference_precision": reference.precision,
        "reference_reduce_mode": reference.reduce_mode,
        "candidate_precision": candidate.precision,
        "candidate_reduce_mode": candidate.reduce_mode,
        "rounds_compared": common,
        "round_count_match": reference.rounds == candidate.rounds,
        "absolute": {
            "loss": percentile_stats(loss_abs),
            "final_parameter": percentile_stats(final_param_abs),
            "combined": percentile_stats(all_abs),
            "max": max_abs,
            "mean": float(np.mean(all_abs)) if all_abs else 0.0,
        },
        "relative": {
            "loss": percentile_stats(loss_rel),
            "final_parameter": percentile_stats(final_param_rel),
            "combined": percentile_stats(all_rel),
            "max": max(all_rel, default=0.0),
            "mean": float(np.mean(all_rel)) if all_rel else 0.0,
        },
        "error_amplification_ratio": float(max_abs / noise_floor),
        "noise_floor": noise_floor,
        "round_specific": round_parameters,
        "final_parameter_spectrum": final_parameters,
        "severity": max_abs,
    }


def _chunk_pair_details(
    reference: TrainingRun,
    candidate: TrainingRun,
) -> list[dict[str, Any]]:
    ref_index = {
        (trace.round_number, trace.chunk_index): trace for trace in reference.chunk_trace
    }
    cand_index = {
        (trace.round_number, trace.chunk_index): trace for trace in candidate.chunk_trace
    }
    common_keys = sorted(set(ref_index) & set(cand_index))
    ref_loss = {index + 1: value for index, value in enumerate(reference.loss_trajectory)}
    cand_loss = {index + 1: value for index, value in enumerate(candidate.loss_trajectory)}
    details: list[dict[str, Any]] = []
    for key in common_keys:
        round_number, chunk_index = key
        ref_trace = ref_index[key]
        cand_trace = cand_index[key]
        ref_gradient = np.asarray(ref_trace.gradient_float(reference.prepared.engine), dtype=np.float64)
        cand_gradient = np.asarray(
            cand_trace.gradient_float(candidate.prepared.engine), dtype=np.float64
        )
        diff = cand_gradient - ref_gradient
        parameter_names = reference.prepared.feature_names
        detail = {
            "round": round_number,
            "chunk_index": chunk_index,
            "local_indices": list(cand_trace.local_indices),
            "global_indices": list(cand_trace.global_indices),
            "row_ids": list(cand_trace.row_ids),
            "sample_count": len(cand_trace.row_ids),
            "loss_reference": ref_loss.get(round_number),
            "loss_candidate": cand_loss.get(round_number),
            "reference_feature_l2": ref_trace.feature_l2,
            "candidate_feature_l2": cand_trace.feature_l2,
            "reference_learning_rate": ref_trace.learning_rate,
            "candidate_learning_rate": cand_trace.learning_rate,
            "gradient_abs_deviation_by_parameter": {
                name: float(abs(diff[index]))
                for index, name in enumerate(parameter_names)
            },
            "gradient_reference_peak_abs": float(np.max(np.abs(ref_gradient)))
            if ref_gradient.size
            else 0.0,
            "gradient_candidate_peak_abs": float(np.max(np.abs(cand_gradient)))
            if cand_gradient.size
            else 0.0,
            "gradient_max_abs_deviation": float(np.max(np.abs(diff))) if diff.size else 0.0,
            "gradient_l1_deviation": float(np.sum(np.abs(diff))),
            "reference_gradient_sha256": ref_trace.to_dict(
                reference.prepared.engine, include_vector=False
            )["gradient_sha256"],
            "candidate_gradient_sha256": cand_trace.to_dict(
                candidate.prepared.engine, include_vector=False
            )["gradient_sha256"],
        }
        details.append(detail)
    return details


def _not_applicable_stage(stage: str, reason: str) -> dict[str, Any]:
    return {
        "stage": stage,
        "applicable": False,
        "reason": reason,
        "comparison_basis": None,
        "metrics_vs_reference": None,
        "incremental_effect": None,
        "severity": 0.0,
        "amplification_ratio": 0.0,
        "round_chunk_details": [],
        "worst_detail": None,
    }


def _stage_result(
    stage: str,
    *,
    reference_truth: TrainingRun,
    candidate_vs_reference: TrainingRun,
    incremental_base: TrainingRun,
    incremental_candidate: TrainingRun,
    comparison_basis: str,
) -> dict[str, Any]:
    metrics_ref = deviation_metrics(reference_truth, candidate_vs_reference)
    incremental = deviation_metrics(incremental_base, incremental_candidate)
    details = _chunk_pair_details(incremental_base, incremental_candidate)
    worst = (
        max(details, key=lambda item: item["gradient_max_abs_deviation"])
        if details
        else None
    )
    return {
        "stage": stage,
        "applicable": True,
        "comparison_basis": comparison_basis,
        "metrics_vs_reference": metrics_ref,
        "incremental_effect": incremental,
        "severity": float(incremental["severity"]),
        "amplification_ratio": float(incremental["error_amplification_ratio"]),
        "round_chunk_details": details,
        "worst_detail": (
            {
                "round": worst["round"],
                "chunk_index": worst["chunk_index"],
                "gradient_max_abs_deviation": worst["gradient_max_abs_deviation"],
                "row_ids": worst["row_ids"],
            }
            if worst is not None
            else None
        ),
    }


def _train_variant(
    dataset: Dataset,
    split: DatasetSplit,
    config: TrainingConfig,
    *,
    precision: str,
    reduce_mode: str,
    stats_precision: str | None = None,
    run_id: str,
) -> tuple[TrainingRun, PreparedData]:
    compute_engine = make_engine(
        precision,
        fixed_scale=config.fixed_scale,
        rounding_mode=config.rounding_mode,
    )
    stats_engine = (
        compute_engine
        if stats_precision is None or stats_precision == precision
        else make_engine(
            stats_precision,
            fixed_scale=config.fixed_scale,
            rounding_mode=config.rounding_mode,
        )
    )
    prepared = prepare_dataset(dataset, split, config, compute_engine, stats_engine)
    run = train_prepared(
        prepared,
        config,
        reduce_mode=reduce_mode,
        run_id=run_id,
        collect_chunk_details=True,
    )
    return run, prepared


def _build_spectrum_once(
    dataset: Dataset,
    split: DatasetSplit,
    config: TrainingConfig,
) -> DiagnosticsResult:
    reference_full, ref_full_prepared = _train_variant(
        dataset,
        split,
        config,
        precision="float64",
        reduce_mode="full",
        run_id="float64/full",
    )
    reference_chunked, _ = _train_variant(
        dataset,
        split,
        config,
        precision="float64",
        reduce_mode="chunked",
        run_id="float64/chunked",
    )

    runs: dict[str, dict[str, TrainingRun]] = {
        "float64": {"full": reference_full, "chunked": reference_chunked}
    }
    prepared: dict[str, PreparedData] = {"float64": ref_full_prepared}

    spectra: dict[str, Any] = {}
    variants: dict[str, Any] = {}
    repeatability: dict[str, Any] = {}

    for precision in PRECISIONS:
        if precision != "float64":
            full_run, prep = _train_variant(
                dataset,
                split,
                config,
                precision=precision,
                reduce_mode="full",
                run_id=f"{precision}/full",
            )
            chunk_run, _ = _train_variant(
                dataset,
                split,
                config,
                precision=precision,
                reduce_mode="chunked",
                run_id=f"{precision}/chunked",
            )
            stats_run, _ = _train_variant(
                dataset,
                split,
                config,
                precision="float64",
                reduce_mode="full",
                stats_precision=precision,
                run_id=f"{precision}/standardization",
            )
            lr_engine = make_engine(
                precision,
                fixed_scale=config.fixed_scale,
                rounding_mode=config.rounding_mode,
            )
            effective_lr = lr_engine.effective_learning_rate(config.learning_rate)
            lr_config = config.replace(learning_rate=effective_lr)
            lr_run, _ = _train_variant(
                dataset,
                split,
                lr_config,
                precision="float64",
                reduce_mode="full",
                run_id=f"{precision}/learning_rate",
            )
        else:
            full_run = reference_full
            chunk_run = reference_chunked
            prep = ref_full_prepared
            stats_run = reference_full
            lr_run = reference_full

        runs.setdefault(precision, {})
        runs[precision]["full"] = full_run
        runs[precision]["chunked"] = chunk_run
        runs[precision]["standardization"] = stats_run
        runs[precision]["learning_rate"] = lr_run
        prepared[precision] = prep

        chunk_full = compare_runs(
            chunk_run,
            full_run,
            tolerance=config.consistency_tolerance,
        )
        variants[precision] = {
            "precision": precision,
            "chunked": chunk_run.summary(),
            "full": full_run.summary(),
            "chunk_full": chunk_full,
            "vs_reference_full": deviation_metrics(reference_full, chunk_run),
            "vs_reference_chunked": deviation_metrics(reference_chunked, chunk_run),
            "fixed_replay": chunk_run.fixed_replay_proof(),
        }
        repeatability[precision] = {
            "chunked_sha256": chunk_run.core_digest,
            "full_sha256": full_run.core_digest,
            "loss_trajectory_hex": chunk_run.losses_hex,
            "params_hex": chunk_run.params_hex,
        }

        standardization_stage = _stage_result(
            "standardization",
            reference_truth=reference_full,
            candidate_vs_reference=stats_run,
            incremental_base=reference_full,
            incremental_candidate=stats_run,
            comparison_basis="target precision normalization statistics/transform; float64 model arithmetic",
        )
        chunk_stage = _stage_result(
            "chunk_accumulation",
            reference_truth=reference_full,
            candidate_vs_reference=chunk_run,
            incremental_base=full_run,
            incremental_candidate=chunk_run,
            comparison_basis="same precision and samples; chunked reduction versus single full reduction",
        )
        lr_stage = _stage_result(
            "learning_rate_scaling",
            reference_truth=reference_full,
            candidate_vs_reference=lr_run,
            incremental_base=reference_full,
            incremental_candidate=lr_run,
            comparison_basis="learning rate represented in target precision, then applied by float64 full training",
        )

        arithmetic_stage: dict[str, Any]
        if precision == "float64":
            arithmetic_stage = _not_applicable_stage(
                "arithmetic_precision", "float64 is the reference arithmetic"
            )
        elif precision == "fixed_point":
            arithmetic_stage = _not_applicable_stage(
                "arithmetic_precision",
                "fixed-point arithmetic error is represented by fixed_point_quantization",
            )
        else:
            arithmetic_stage = _stage_result(
                "arithmetic_precision",
                reference_truth=reference_full,
                candidate_vs_reference=full_run,
                incremental_base=reference_full,
                incremental_candidate=full_run,
                comparison_basis="all model arithmetic and model storage in target precision; full reduction",
            )

        if precision != "fixed_point":
            quant_stage = _not_applicable_stage(
                "fixed_point_quantization", "only applicable to the fixed_point precision tier"
            )
            rounding_stage = _not_applicable_stage(
                "rounding_mode", "only applicable to the fixed_point precision tier"
            )
        else:
            quant_stage = _stage_result(
                "fixed_point_quantization",
                reference_truth=reference_full,
                candidate_vs_reference=full_run,
                incremental_base=stats_run,
                incremental_candidate=full_run,
                comparison_basis=(
                    "fixed-point arithmetic/storage with configured scale and rounding "
                    "versus the same fixed-point-normalized features trained in float64"
                ),
            )
            alt_mode = alternate_rounding_mode(config.rounding_mode)
            alt_config = config.replace(rounding_mode=alt_mode)
            alt_run, _ = _train_variant(
                dataset,
                split,
                alt_config,
                precision="fixed_point",
                reduce_mode="full",
                run_id="fixed_point/rounding_alt",
            )
            runs["fixed_point"]["rounding_alt"] = alt_run
            rounding_stage = _stage_result(
                "rounding_mode",
                reference_truth=reference_full,
                candidate_vs_reference=alt_run,
                incremental_base=full_run,
                incremental_candidate=alt_run,
                comparison_basis=(
                    f"fixed-point full training with {alt_mode} versus configured {config.rounding_mode}"
                ),
            )

        spectra[precision] = {
            "precision": precision,
            "stages": {
                "standardization": standardization_stage,
                "chunk_accumulation": chunk_stage,
                "fixed_point_quantization": quant_stage,
                "rounding_mode": rounding_stage,
                "learning_rate_scaling": lr_stage,
                "arithmetic_precision": arithmetic_stage,
            },
        }

    dominant = _select_dominant(spectra)
    switch_contract = _switch_contract(variants, prepared, config)
    if not (
        switch_contract["chunk_plan_identical"]
        and switch_contract["train_indexes_identical"]
        and switch_contract["validation_indexes_identical"]
    ):
        raise inconsistent(
            "precision_switch_contract_mismatch",
            "precision switching changed the chunk plan or sample index sets",
            contract=switch_contract,
        )
    index_checks = _sample_index_checks(prepared, runs)
    if not index_checks["consistent"]:
        raise inconsistent(
            "sample_index_set_mismatch",
            "sample index sets differ across precision/reduction stages",
            **index_checks,
        )

    payload = {
        "reference": {
            "precision": "float64",
            "reduce_mode": "full",
            "run_sha256": reference_full.core_digest,
            "trajectory_sha256": reference_full.trajectory_digest(),
            "final_loss": reference_full.loss_trajectory[-1],
        },
        "spectra": spectra,
        "dominant_source": dominant,
        "precision_switch": {
            "contract": switch_contract,
            "variants": variants,
        },
        "sample_index_checks": index_checks,
        "repeatability_contract": repeatability,
    }
    return DiagnosticsResult(
        payload=payload,
        runs=runs,
        prepared=prepared,
        reference_full=reference_full,
        reference_chunked=reference_chunked,
    )


def _select_dominant(spectra: dict[str, Any]) -> dict[str, Any]:
    best: dict[str, Any] | None = None
    best_key: tuple[float, str, str] | None = None
    for precision in PRECISIONS:
        stages = spectra[precision]["stages"]
        for stage_name, stage in stages.items():
            if not stage["applicable"]:
                continue
            severity = float(stage["severity"])
            sort_key = (severity, precision, stage_name)
            if best is None or sort_key > best_key:  # type: ignore[operator]
                best = {
                    "precision": precision,
                    "stage": stage_name,
                    "severity": severity,
                    "amplification_ratio": stage["amplification_ratio"],
                    "worst_detail": stage["worst_detail"],
                    "detail_count": len(stage["round_chunk_details"]),
                    "detail_location": (
                        f"error_spectrum.spectra.{precision}.stages.{stage_name}."
                        "round_chunk_details"
                    ),
                    "round_chunk_details": stage["round_chunk_details"],
                    "metrics_vs_reference": stage["metrics_vs_reference"],
                    "incremental_effect": stage["incremental_effect"],
                }
                best_key = sort_key
    assert best is not None
    return best


def _switch_contract(
    variants: dict[str, Any],
    prepared: dict[str, PreparedData],
    config: TrainingConfig,
) -> dict[str, Any]:
    chunk_hashes = {
        precision: prepared[precision].chunk_plan_fingerprint["sha256"]
        for precision in PRECISIONS
    }
    train_hashes = {
        precision: prepared[precision].sample_fingerprint["train_sha256"]
        for precision in PRECISIONS
    }
    validation_hashes = {
        precision: prepared[precision].sample_fingerprint["validation_sha256"]
        for precision in PRECISIONS
    }
    return {
        "chunk_plan_sha256_by_precision": chunk_hashes,
        "train_index_sha256_by_precision": train_hashes,
        "validation_index_sha256_by_precision": validation_hashes,
        "chunk_size": config.chunk_size,
        "reduction_order": (
            "chunk_index_ascending; within_chunk_sample_index_ascending; "
            "sequential_left_fold_over_chunk_sums"
        ),
        "full_reduction_order": "sample_index_ascending; single vectorized reduction",
        "convergence_strategy": config.convergence_strategy,
        "convergence_tolerance": config.convergence_tolerance,
        "min_improvement": config.min_improvement,
        "precision_switch_changes_only_precision_loss": True,
        "chunk_plan_identical": len(set(chunk_hashes.values())) == 1,
        "train_indexes_identical": len(set(train_hashes.values())) == 1,
        "validation_indexes_identical": len(set(validation_hashes.values())) == 1,
    }


def _sample_index_checks(
    prepared: dict[str, PreparedData],
    runs: dict[str, dict[str, TrainingRun]],
) -> dict[str, Any]:
    base = prepared["float64"]
    expected_train = tuple(base.train_global_indices)
    expected_validation = tuple(base.validation_global_indices)
    issues: list[dict[str, Any]] = []
    run_fingerprints: dict[str, Any] = {}
    for precision, modes in runs.items():
        for mode, run in modes.items():
            key = f"{precision}/{mode}"
            run_fingerprints[key] = {
                "train_sha256": run.prepared.sample_fingerprint["train_sha256"],
                "validation_sha256": run.prepared.sample_fingerprint["validation_sha256"],
                "chunk_plan_sha256": run.prepared.chunk_plan_fingerprint["sha256"],
            }
            if (
                run.prepared.sample_fingerprint["train_sha256"]
                != base.sample_fingerprint["train_sha256"]
            ):
                issues.append({"run": key, "category": "train_index_set_mismatch"})
            if (
                run.prepared.sample_fingerprint["validation_sha256"]
                != base.sample_fingerprint["validation_sha256"]
            ):
                issues.append({"run": key, "category": "validation_index_set_mismatch"})

    chunk_sets: dict[str, Any] = {}
    for precision, prep in prepared.items():
        flattened = tuple(index for chunk in prep.chunk_global_indices for index in chunk)
        duplicates = len(flattened) - len(set(flattened))
        exact = flattened == expected_train
        chunk_sets[precision] = {
            "chunk_count": prep.chunk_count,
            "flattened_sha256": sha256_json(list(flattened)),
            "duplicate_indices": duplicates,
            "covers_train_exactly": exact,
            "chunk_plan_sha256": prep.chunk_plan_fingerprint["sha256"],
        }
        if not exact:
            issues.append({"precision": precision, "category": "chunk_coverage_mismatch"})
        if duplicates:
            issues.append({"precision": precision, "category": "chunk_overlap"})
        if tuple(prep.validation_global_indices) != expected_validation:
            issues.append(
                {"precision": precision, "category": "validation_index_set_mismatch"}
            )
    return {
        "consistent": not issues,
        "train_sha256": base.sample_fingerprint["train_sha256"],
        "validation_sha256": base.sample_fingerprint["validation_sha256"],
        "expected_train_indices": list(expected_train),
        "expected_validation_indices": list(expected_validation),
        "chunk_sets": chunk_sets,
        "run_fingerprints": run_fingerprints,
        "issues": issues,
    }


def _spectrum_digest(payload: dict[str, Any]) -> str:
    return sha256_json(payload)


def build_diagnostics(
    dataset: Dataset,
    split: DatasetSplit,
    config: TrainingConfig,
    *,
    verify_recompute: bool = True,
) -> DiagnosticsResult:
    first = _build_spectrum_once(dataset, split, config)
    first_digest = _spectrum_digest(first.payload)
    first_run_digests = {
        f"{precision}/{mode}": run.core_digest
        for precision, modes in first.runs.items()
        for mode, run in modes.items()
    }
    verification: dict[str, Any] = {
        "ran": False,
        "trajectory_recompute_match": None,
        "error_spectrum_recompute_match": None,
        "first_spectrum_sha256": first_digest,
        "recomputed_spectrum_sha256": None,
        "mismatches": [],
    }
    if verify_recompute:
        second = _build_spectrum_once(dataset, split, config)
        second_digest = _spectrum_digest(second.payload)
        second_run_digests = {
            f"{precision}/{mode}": run.core_digest
            for precision, modes in second.runs.items()
            for mode, run in modes.items()
        }
        trajectory_match = first_run_digests == second_run_digests
        spectrum_match = first_digest == second_digest
        repeatability_by_precision: dict[str, Any] = {}
        for tier in PRECISIONS:
            tier_result: dict[str, Any] = {}
            for mode in ("chunked", "full"):
                first_run = first.runs[tier][mode]
                second_run = second.runs[tier][mode]
                tier_result[mode] = {
                    "byte_identical": first_run.core_digest == second_run.core_digest,
                    "first_core_sha256": first_run.core_digest,
                    "second_core_sha256": second_run.core_digest,
                    "loss_trajectory_identical": first_run.losses_hex == second_run.losses_hex,
                    "params_identical": first_run.params_hex == second_run.params_hex,
                    "rounds_identical": first_run.rounds == second_run.rounds,
                    "validation_identical": (
                        first_run.validation_trajectory == second_run.validation_trajectory
                    ),
                }
            tier_result["fixed_replay_recomputable"] = (
                (first.runs[tier]["chunked"].fixed_replay_proof() or {}).get(
                    "recomputed_final_matches"
                )
                if tier == "fixed_point"
                else None
            )
            repeatability_by_precision[tier] = tier_result
        verification.update(
            {
                "ran": True,
                "trajectory_recompute_match": trajectory_match,
                "error_spectrum_recompute_match": spectrum_match,
                "recomputed_spectrum_sha256": second_digest,
                "trajectory_digests": first_run_digests,
                "repeatability_by_precision": repeatability_by_precision,
            }
        )
        if not trajectory_match:
            raise inconsistent(
                "trajectory_recompute_mismatch",
                "training trajectory did not reproduce byte-identically",
                first_run_digests=first_run_digests,
                second_run_digests=second_run_digests,
            )
        if not spectrum_match:
            raise inconsistent(
                "error_spectrum_recompute_mismatch",
                "error spectrum did not reproduce deterministically",
                first_sha256=first_digest,
                second_sha256=second_digest,
            )
    first.payload["verification"] = verification
    return first


def require_tolerance_consistency(
    diagnostics: DiagnosticsResult,
    *,
    precision: str | None = None,
) -> None:
    variants = diagnostics.payload["precision_switch"]["variants"]
    selected = [precision] if precision is not None else list(PRECISIONS)
    for tier in selected:
        comparison = variants[tier]["chunk_full"]
        if not comparison["within_tolerance"]:
            failure = comparison["first_exceedance"]
            raise tolerance_exceeded(
                precision=tier,
                round_number=(
                    int(failure["round"])
                    if isinstance(failure.get("round"), int)
                    else comparison["rounds_compared"] + 1
                ),
                location=str(failure["location"]),
                observed=float(failure.get("observed", 0.0)),
                tolerance=float(comparison["tolerance"]),
                parameter=failure.get("parameter"),
            )


def chunk_full_summary(diagnostics: DiagnosticsResult) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for precision in PRECISIONS:
        comparison = diagnostics.payload["precision_switch"]["variants"][precision][
            "chunk_full"
        ]
        payload[precision] = {
            "within_tolerance": comparison["within_tolerance"],
            "max_round_abs_deviation": comparison["max_round_abs_deviation"],
            "final_parameter_max_abs_deviation": comparison[
                "final_parameter_max_abs_deviation"
            ],
            "convergence_rounds_equal": comparison["convergence_rounds_equal"],
            "candidate_convergence_round": comparison[
                "candidate_convergence_round"
            ],
            "reference_convergence_round": comparison[
                "reference_convergence_round"
            ],
            "first_exceedance": comparison["first_exceedance"],
        }
    return payload
