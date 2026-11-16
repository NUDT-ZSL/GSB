"""Cross-run numeric drift checks and reference persistence."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from .atomic import atomic_write_json, read_json
from .errors import E_NUMERIC_DRIFT, Failure

REFERENCE_SCHEMA_VERSION = 1


def compare_numeric_results(
    reference: dict[str, Any],
    candidate: dict[str, Any],
    tolerance: float,
) -> tuple[dict[str, Any], Failure | None]:
    reference_trajectory = reference.get("loss_trajectory", [])
    candidate_trajectory = candidate.get("loss_trajectory", [])
    max_loss_difference = 0.0
    max_coefficient_difference = 0.0
    structural_mismatch = len(reference_trajectory) != len(candidate_trajectory)

    if not structural_mismatch:
        try:
            for reference_round, candidate_round in zip(reference_trajectory, candidate_trajectory):
                for key in ("data_loss", "regularization_loss", "total_loss", "update_norm"):
                    difference = abs(float(reference_round[key]) - float(candidate_round[key]))
                    max_loss_difference = max(max_loss_difference, difference)
        except (KeyError, TypeError, ValueError):
            structural_mismatch = True

    reference_final = reference.get("final_coefficients", {})
    candidate_final = candidate.get("final_coefficients", {})
    reference_weights = reference_final.get("weights", [])
    candidate_weights = candidate_final.get("weights", [])
    if len(reference_weights) != len(candidate_weights):
        structural_mismatch = True
    else:
        try:
            for reference_weight, candidate_weight in zip(reference_weights, candidate_weights):
                max_coefficient_difference = max(
                    max_coefficient_difference,
                    abs(float(reference_weight) - float(candidate_weight)),
                )
            max_coefficient_difference = max(
                max_coefficient_difference,
                abs(float(reference_final.get("intercept", 0.0)) - float(candidate_final.get("intercept", 0.0))),
            )
        except (TypeError, ValueError):
            structural_mismatch = True

    within_tolerance = (
        not structural_mismatch
        and max_loss_difference <= tolerance
        and max_coefficient_difference <= tolerance
        and math.isfinite(max_loss_difference)
        and math.isfinite(max_coefficient_difference)
    )
    audit = {
        "reference_found": True,
        "tolerance": tolerance,
        "max_abs_loss_difference": max_loss_difference,
        "max_abs_final_coefficient_difference": max_coefficient_difference,
        "structural_mismatch": structural_mismatch,
        "within_tolerance": within_tolerance,
    }
    if within_tolerance:
        return audit, None
    failure = Failure(
        business_code=E_NUMERIC_DRIFT,
        phase="numeric_verification",
        stage="reference_compare",
        message="loss trajectory or final coefficients exceeded the declared numeric tolerance",
        exception_type="NumericDriftError",
        scheme=candidate.get("scheme"),
        round=len(candidate_trajectory) if structural_mismatch else None,
        details=audit,
    )
    return audit, failure


class NumericReferenceStore:
    def __init__(self, state_dir: str | Path):
        self.directory = Path(state_dir).expanduser().resolve() / "numeric_references"
        self.directory.mkdir(parents=True, exist_ok=True)

    def _path(self, training_fingerprint: str) -> Path:
        return self.directory / f"{training_fingerprint}.json"

    def load(self, training_fingerprint: str) -> dict[str, Any] | None:
        path = self._path(training_fingerprint)
        if not path.exists():
            return None
        try:
            payload = read_json(path)
            if isinstance(payload, dict) and payload.get("reference_schema_version") == REFERENCE_SCHEMA_VERSION:
                result = payload.get("result")
                return result if isinstance(result, dict) else None
            return None
        except Exception:
            return None

    def store(self, training_fingerprint: str, result: dict[str, Any]) -> None:
        payload = {
            "reference_schema_version": REFERENCE_SCHEMA_VERSION,
            "training_fingerprint": training_fingerprint,
            "result": {
                "final_coefficients": result["final_coefficients"],
                "loss_trajectory": result["loss_trajectory"],
            },
        }
        atomic_write_json(self._path(training_fingerprint), payload, indent=2)
