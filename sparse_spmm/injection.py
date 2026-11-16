"""Isolated, independently recorded exception-injection samples."""

from __future__ import annotations

import copy
from typing import Any

import numpy as np

from .attribution import precision_settings
from .csr import CSRMatrix, validate_csr
from .dense import validate_finite, validate_shapes
from .errors import ConfigError, InjectionMissError, SpMMError
from .kernels import validate_block_size
from .runner import CaseInputs


def _clone_csr(csr: CSRMatrix) -> CSRMatrix:
    return CSRMatrix(
        shape=csr.shape,
        indptr=csr.indptr.copy(),
        indices=csr.indices.copy(),
        data=csr.data.copy(),
        nonzero_ids=list(csr.nonzero_ids),
        generation_ordinals=csr.generation_ordinals.copy(),
    )


def _parse_nonfinite(value: str) -> float:
    normalized = str(value).strip().lower()
    mapping = {"nan": np.nan, "inf": np.inf, "+inf": np.inf, "-inf": -np.inf}
    if normalized not in mapping:
        raise ConfigError(
            "injected non-finite value must be nan, inf or -inf",
            stage="injection-config",
            details={"value": value},
        )
    return float(mapping[normalized])


def _apply_injection(case: CaseInputs, item: dict[str, Any]) -> None:
    kind = item.get("kind")
    if kind == "nonfinite_b":
        B = case.B.copy()
        target = item.get("target", {})
        row = int(target.get("row", 0))
        col = int(target.get("col", 0))
        if not (0 <= row < B.shape[0] and 0 <= col < B.shape[1]):
            raise ConfigError(
                "nonfinite_b target is outside B",
                stage="injection-config",
                details={"target": target, "shape": list(B.shape)},
            )
        B[row, col] = np.float32(_parse_nonfinite(item.get("value", "nan")))
        validate_shapes(case.csr, B, case.C, stage=item.get("stage", "injected-shape-validation"))
        validate_finite(case.csr, B, case.C, stage=item.get("stage", "injected-finite-validation"))
        return
    if kind == "nonfinite_a":
        csr = _clone_csr(case.csr)
        position = int(item.get("position", 0))
        if not 0 <= position < csr.nnz:
            raise ConfigError("nonfinite_a position is outside A.data", stage="injection-config")
        csr.data[position] = np.float32(_parse_nonfinite(item.get("value", "nan")))
        validate_finite(csr, case.B, case.C, stage=item.get("stage", "injected-finite-validation"))
        return
    if kind == "csr_indptr_mismatch":
        csr = _clone_csr(case.csr)
        csr.indptr[-1] = int(csr.indptr[-1]) + int(item.get("delta", 1))
        validate_csr(csr, stage=item.get("stage", "injected-csr-validation"))
        return
    if kind == "b_shape_mismatch":
        B = case.B
        drop_rows = int(item.get("drop_rows", 1))
        if not 1 <= drop_rows < B.shape[0]:
            raise ConfigError("b_shape_mismatch drop_rows is invalid", stage="injection-config")
        B_bad = B[:-drop_rows].copy()
        validate_shapes(case.csr, B_bad, case.C, stage=item.get("stage", "injected-shape-validation"))
        return
    if kind == "c_shape_mismatch":
        C_bad = case.C[:, :-1].copy() if case.C.shape[1] > 1 else case.C[: max(1, case.C.shape[0] - 1)].copy()
        validate_shapes(case.csr, case.B, C_bad, stage=item.get("stage", "injected-shape-validation"))
        return
    if kind == "block_size_out_of_range":
        validate_block_size(
            item.get("block_size"),
            K=case.csr.cols,
            stage=item.get("stage", "injected-block-validation"),
        )
        return
    if kind == "tolerance_zero":
        config = copy.deepcopy(case.sample_config)
        config.setdefault("execution", {}).setdefault("tolerance", {})["relative"] = 0.0
        precision_settings(config)
        return
    raise ConfigError(
        "unknown injection kind",
        stage="injection-config",
        details={"kind": repr(kind)},
    )


def run_injections(case: CaseInputs, injection_config: dict[str, Any]) -> dict[str, Any]:
    items = injection_config.get("injections", [])
    if not isinstance(items, list):
        raise ConfigError("injections must be a list", stage="injection-config")
    records: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ConfigError("injection item must be an object", stage="injection-config")
        injection_id = str(item.get("id", f"INJ-{index + 1:03d}"))
        expected_code = str(item.get("expected_code", ""))
        record: dict[str, Any] = {
            "injection_id": injection_id,
            "kind": item.get("kind"),
            "expected_code": expected_code,
            "hit": False,
            "actual_code": None,
            "exception_type": None,
            "stage": item.get("stage"),
        }
        try:
            _apply_injection(case, item)
            miss = InjectionMissError(
                "injection completed without raising the expected error",
                stage=str(item.get("stage", "injection")),
                details={"injection_id": injection_id, "expected_code": expected_code},
            )
            failure = miss.to_failure(injection_id=injection_id)
            failures.append(failure)
            record["actual_code"] = miss.business_code
            record["exception_type"] = type(miss).__name__
        except SpMMError as exc:
            failure = exc.to_failure(injection_id=injection_id)
            failures.append(failure)
            record["actual_code"] = exc.business_code
            record["exception_type"] = type(exc).__name__
            record["hit"] = bool(expected_code and exc.business_code == expected_code)
        except Exception as exc:
            failure = {
                "business_code": "E_INTERNAL",
                "exception_type": type(exc).__name__,
                "stage": str(item.get("stage", "injection")),
                "shape": {"A": [case.csr.rows, case.csr.cols], "B": list(case.B.shape), "C": list(case.C.shape)},
                "message": str(exc),
                "details": {"injection_id": injection_id},
                "injection_id": injection_id,
            }
            failures.append(failure)
            record["actual_code"] = "E_INTERNAL"
            record["exception_type"] = type(exc).__name__
        records.append(record)
    attempted_ids = [record["injection_id"] for record in records]
    hit_ids = [record["injection_id"] for record in records if record["hit"]]
    expected_ids = [
        record["injection_id"]
        for record in records
        if record["expected_code"] and record["expected_code"] == record["actual_code"]
    ]
    return {
        "attempted_count": len(records),
        "hit_count": len(hit_ids),
        "attempted_ids": attempted_ids,
        "hit_ids": hit_ids,
        "expected_hit_ids": expected_ids,
        "all_expected_codes_hit": len(expected_ids) == len(records),
        "records": records,
        "failures": failures,
    }
