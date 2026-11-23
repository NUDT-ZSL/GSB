"""Error propagation analysis driver and the four public entry points."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Dict, List, Optional, Sequence

from .boosting import BoostingModel, train_boosting
from .config import TrainConfig
from .data import Dataset
from .error_analysis import STAGE_NAMES, analyse_round, build_error_report
from .errors import ErrorCode
from .injection import InjectionSpec
from .verifier import VerificationResult, verify_reproducibility


def run_error_analysis(
    dataset: Dataset,
    candidate_config: TrainConfig,
    *,
    injection_specs: Optional[Sequence[InjectionSpec]] = None,
) -> Dict[str, Any]:
    """Compare the configured pipeline against a float64 reference run.

    The reference uses identical trees-count/depth/sampling but forces
    ``float64`` and disables reduced precision effects.  Both runs consume
    the same sampled row/column sets because the master seed is unchanged.
    """
    reference_config = replace(
        candidate_config,
        precision="float64",
        rounding="half_even",
        subnormal_protection=False,
    )
    reference_model = train_boosting(
        dataset, reference_config, injection_specs=injection_specs
    )
    candidate_model = train_boosting(
        dataset, candidate_config, injection_specs=injection_specs
    )

    per_round: List[Dict[str, Any]] = []
    all_records: List[Dict[str, Any]] = []
    previous_rel: Optional[float] = None
    for round_index, (ref_detail, cand_detail) in enumerate(
        zip(reference_model.round_details, candidate_model.round_details)
    ):
        deviations, records, dominant = analyse_round(
            round_index,
            ref_detail.checkpoints,
            cand_detail.checkpoints,
            candidate_config,
            previous_rel,
        )
        all_records.extend(records)
        max_rel = max(item.rel_deviation for item in deviations)
        previous_rel = max_rel
        per_round.append(
            {
                "round_index": round_index,
                "deviations": [item.to_dict() for item in deviations],
                "dominant_stage": dominant,
                "max_rel_deviation": float(max_rel),
            }
        )

    report = build_error_report(per_round)
    report["records"] = all_records
    report["reference_precision"] = "float64"
    report["candidate_precision"] = candidate_config.precision
    report["exceeded"] = bool(all_records)
    report["code"] = (
        ErrorCode.TOLERANCE_EXCEEDED.value
        if all_records
        else ErrorCode.OK.value
    )
    return report


def summarise_records(model: BoostingModel) -> Dict[str, Any]:
    """Aggregate stage records, degradations and unattributed faults."""
    stage_counts: Dict[str, int] = {}
    code_counts: Dict[str, int] = {}
    degradations: List[Dict[str, Any]] = []
    unattributed: List[Dict[str, Any]] = []
    for record in model.records:
        stage_counts[record["stage"]] = stage_counts.get(record["stage"], 0) + 1
        code_counts[record["code"]] = code_counts.get(record["code"], 0) + 1
        if record["degradation"] is not None:
            degradations.append(
                {
                    "stage": record["stage"],
                    "code": record["code"],
                    "degradation": record["degradation"],
                    "sample_indices": record["sample_indices"],
                }
            )
        if record["code"] == ErrorCode.UNATTRIBUTED.value:
            unattributed.append(record)
    return {
        "stage_counts": stage_counts,
        "code_counts": code_counts,
        "degradations": degradations,
        "unattributed": unattributed,
        "code": (
            ErrorCode.UNATTRIBUTED.value if unattributed else ErrorCode.OK.value
        ),
    }


def run_full_diagnostic(
    dataset: Dataset,
    config: TrainConfig,
    *,
    injection_specs: Optional[Sequence[InjectionSpec]] = None,
) -> Dict[str, Any]:
    """Train, analyse error propagation and verify reproducibility."""
    model = train_boosting(
        dataset, config, injection_specs=injection_specs
    )
    error_report = run_error_analysis(
        dataset, config, injection_specs=injection_specs
    )
    verification: VerificationResult = verify_reproducibility(
        dataset,
        config,
        injection_specs=injection_specs,
        error_report=error_report,
    )
    attribution = summarise_records(model)
    return {
        "training": model.to_report(),
        "error_analysis": error_report,
        "verification": verification.to_dict(),
        "attribution": attribution,
    }


__all__ = [
    "STAGE_NAMES",
    "run_error_analysis",
    "run_full_diagnostic",
    "summarise_records",
]
