"""Reproducibility and recomputation verifier.

The verifier trains the identical configuration twice and asserts:

* per-round losses, serialised tree structures and prediction hashes match
  byte-for-byte across the two runs;
* the row/column sampling index sets agree round by round;
* every round prediction equals the base prediction plus independently
  re-accumulated per-tree contributions;
* error-attribution records reference deviation details that exist in the
  error analysis report.

The first mismatch returns ``E_INCONSISTENT`` with the first offending
round and pipeline step.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

import numpy as np

from .boosting import (
    recompute_prediction,
    train_boosting,
)
from .config import TrainConfig
from .data import Dataset
from .errors import ErrorCode
from .injection import InjectionSpec
from .io_utils import stable_hash
from .precision import quantize


@dataclass
class VerificationResult:
    passed: bool
    code: str
    first_mismatch: Optional[Dict[str, Any]]
    checks: List[Dict[str, Any]] = field(default_factory=list)
    run_a_hash: Optional[str] = None
    run_b_hash: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "passed": bool(self.passed),
            "code": self.code,
            "first_mismatch": self.first_mismatch,
            "checks": self.checks,
            "run_a_hash": self.run_a_hash,
            "run_b_hash": self.run_b_hash,
        }


def _failure(
    result: VerificationResult,
    step: str,
    round_index: Optional[int],
    message: str,
    *,
    expected: Any = None,
    actual: Any = None,
) -> VerificationResult:
    result.passed = False
    result.code = ErrorCode.INCONSISTENT.value
    result.first_mismatch = {
        "round_index": round_index,
        "step": step,
        "message": message,
        "expected": expected,
        "actual": actual,
    }
    return result


def verify_reproducibility(
    dataset: Dataset,
    config: TrainConfig,
    *,
    injection_specs: Optional[Sequence[InjectionSpec]] = None,
    error_report: Optional[Dict[str, Any]] = None,
) -> VerificationResult:
    """Run two trainings and cross-check every reproducibility contract."""
    model_a = train_boosting(
        dataset, config, injection_specs=injection_specs
    )
    model_b = train_boosting(
        dataset, config, injection_specs=injection_specs
    )
    result = VerificationResult(
        passed=True,
        code=ErrorCode.OK.value,
        first_mismatch=None,
    )
    result.run_a_hash = stable_hash(model_a.to_report())
    result.run_b_hash = stable_hash(model_b.to_report())

    def check(
        step: str,
        round_index: Optional[int],
        condition: bool,
        message: str,
        *,
        expected: Any = None,
        actual: Any = None,
    ) -> bool:
        result.checks.append(
            {
                "step": step,
                "round_index": round_index,
                "passed": bool(condition),
                "message": message,
            }
        )
        if not condition and result.passed:
            _failure(
                result,
                step,
                round_index,
                message,
                expected=expected,
                actual=actual,
            )
        return condition

    check(
        "full_report_hash",
        None,
        result.run_a_hash == result.run_b_hash,
        "canonical report hashes of the two runs match",
        expected=result.run_a_hash,
        actual=result.run_b_hash,
    )

    if len(model_a.round_details) != len(model_b.round_details):
        _failure(
            result,
            "round_count",
            None,
            "round counts differ",
            expected=len(model_a.round_details),
            actual=len(model_b.round_details),
        )
        return result

    for round_index, (detail_a, detail_b) in enumerate(
        zip(model_a.round_details, model_b.round_details)
    ):
        check(
            "loss_before",
            round_index,
            detail_a.loss_before == detail_b.loss_before,
            "per-round starting loss is identical",
            expected=detail_a.loss_before,
            actual=detail_b.loss_before,
        )
        check(
            "loss_after",
            round_index,
            detail_a.loss_after == detail_b.loss_after,
            "per-round ending loss is identical",
            expected=detail_a.loss_after,
            actual=detail_b.loss_after,
        )
        check(
            "tree_structure_hash",
            round_index,
            stable_hash(detail_a.tree.to_dict())
            == stable_hash(detail_b.tree.to_dict()),
            "serialised tree structure is byte-identical",
            expected=stable_hash(detail_a.tree.to_dict()),
            actual=stable_hash(detail_b.tree.to_dict()),
        )
        check(
            "prediction_hash",
            round_index,
            stable_hash(detail_a.prediction_after)
            == stable_hash(detail_b.prediction_after),
            "per-round prediction hash is identical",
            expected=stable_hash(detail_a.prediction_after),
            actual=stable_hash(detail_b.prediction_after),
        )
        check(
            "row_sample_set",
            round_index,
            detail_a.row_sample == detail_b.row_sample,
            "row sampling index sets are identical and ordered",
            expected=detail_a.row_sample,
            actual=detail_b.row_sample,
        )
        check(
            "col_sample_set",
            round_index,
            detail_a.col_sample == detail_b.col_sample,
            "column sampling index sets are identical and ordered",
            expected=detail_a.col_sample,
            actual=detail_b.col_sample,
        )

    # Independent recomputation from base prediction and tree contributions.
    recomputed = recompute_prediction(model_a)
    check(
        "independent_prediction_recompute",
        None,
        stable_hash(recomputed) == stable_hash(model_a.final_prediction),
        "final prediction is rebuilt independently from stored tree details",
        expected=stable_hash(model_a.final_prediction),
        actual=stable_hash(recomputed),
    )

    cumulative = model_a.base_prediction.copy()
    excluded = np.asarray(
        sorted(model_a.binnings.excluded_rows), dtype=np.int64
    )
    for round_index, detail in enumerate(model_a.round_details):
        unscaled = detail.tree.predict_rows(model_a.dataset.X)
        contribution = quantize(
            unscaled * detail.learning_rate, model_a.config
        )
        if excluded.size:
            contribution[excluded] = 0.0
        cumulative = quantize(
            cumulative + contribution, model_a.config
        )
        check(
            "round_prefix_recompute",
            round_index,
            stable_hash(cumulative)
            == stable_hash(detail.prediction_after),
            "round prediction equals base plus the prefix of tree contributions",
            expected=stable_hash(detail.prediction_after),
            actual=stable_hash(cumulative),
        )

    # Error attribution records must trace back to deviation details.
    if error_report is not None:
        violation_keys = {
            (
                violation["stage"],
                violation["round_index"],
                violation["sample_index"],
            )
            for violation in error_report.get("violations", [])
        }
        for round_block in error_report.get("rounds", []):
            for deviation in round_block["deviations"]:
                for violation in deviation["violations"]:
                    key = (
                        violation["stage"],
                        violation["round_index"],
                        violation["sample_index"],
                    )
                    check(
                        "error_attribution_trace",
                        violation["round_index"],
                        key in violation_keys,
                        "tolerance violation is traceable to deviation detail",
                        expected=True,
                        actual=key,
                    )

    return result
