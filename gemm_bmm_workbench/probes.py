from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import numpy as np

from .canonical import atomic_write_json
from .errors import Codes, WorkbenchError
from .kernels import tiled_multiply_accumulate, verify_against_reference
from .ledger import guard_schema_transition
from .resources import CommitLock
from .runtime import Workbench
from .specs import check_duplicate_job_ids, check_revision_progress, normalize_job


def _expect_error(name: str, expected_code: str, action: Callable[[], Any], bench: Workbench) -> dict[str, Any]:
    try:
        action()
    except WorkbenchError as exc:
        record = bench.record_error(exc, source=f"probe:{name}")
        return {
            "name": name,
            "expected_code": expected_code,
            "actual_code": exc.code,
            "passed": exc.code == expected_code,
            "message": exc.message,
            "details": exc.details or {},
            "log_seq": record.get("seq"),
        }
    raise AssertionError(f"probe {name} did not raise {expected_code}")


def run_diagnostic_probes(bench: Workbench) -> dict[str, Any]:
    probes: list[dict[str, Any]] = []

    def invalid_shape() -> None:
        normalize_job(
            {
                "job_id": "probe_shape",
                "op_kind": "gemm",
                "matrix_shapes": {"A": [2, 3], "B": [4, 5]},
                "block_size": 2,
            }
        )

    def batch_mismatch() -> None:
        normalize_job(
            {
                "job_id": "probe_batch",
                "op_kind": "bmm",
                "matrix_shapes": {"A": [2, 4, 3], "B": [3, 3, 5]},
                "block_size": 2,
            }
        )

    def bad_dtype() -> None:
        normalize_job(
            {
                "job_id": "probe_dtype",
                "op_kind": "gemm",
                "matrix_shapes": {"A": [2, 2], "B": [2, 2]},
                "dtype": "int8",
                "block_size": 1,
            }
        )

    def bad_layout() -> None:
        normalize_job(
            {
                "job_id": "probe_layout",
                "op_kind": "gemm",
                "matrix_shapes": {"A": [2, 2], "B": [2, 2]},
                "layout": "tiled_weird",
                "block_size": 1,
            }
        )

    def bad_block() -> None:
        normalize_job(
            {
                "job_id": "probe_block",
                "op_kind": "gemm",
                "matrix_shapes": {"A": [2, 2], "B": [2, 2]},
                "block_size": 0,
            }
        )

    def duplicate() -> None:
        check_duplicate_job_ids([{"job_id": "dup"}, {"job_id": "dup"}])

    def revision_regression() -> None:
        check_revision_progress(
            [{"job_id": "probe_revision", "revision": 1}],
            {"probe_revision": {"revision": 2}},
        )

    def ledger_regression() -> None:
        guard_schema_transition(2, 1)

    def resource_limit() -> None:
        limited = Workbench(
            bench.state_dir,
            budget={
                "max_jobs_per_stage": 1,
                "max_accum_buffer_bytes": bench.budget.max_accum_buffer_bytes,
                "max_parallel_jobs": 1,
                "resource_policy": "error",
            },
        )
        limited.submit_records(
            [
                {
                    "job_id": "resource_a",
                    "op_kind": "gemm",
                    "matrix_shapes": {"A": [2, 2], "B": [2, 2], "C": [2, 2]},
                    "block_size": 1,
                    "seed": 1,
                },
                {
                    "job_id": "resource_b",
                    "op_kind": "gemm",
                    "matrix_shapes": {"A": [2, 2], "B": [2, 2], "C": [2, 2]},
                    "block_size": 1,
                    "seed": 2,
                },
            ]
        )

    def stage_busy() -> None:
        lock = CommitLock(bench.state_dir / "locks" / "commit.lock").acquire()
        try:
            CommitLock(bench.state_dir / "locks" / "commit.lock").acquire()
        finally:
            lock.release()

    probes.append(_expect_error("invalid_shape", Codes.SHAPE_INVALID, invalid_shape, bench))
    probes.append(_expect_error("batch_dimension_mismatch", Codes.SHAPE_INVALID, batch_mismatch, bench))
    probes.append(_expect_error("unsupported_dtype", Codes.DTYPE_UNSUPPORTED, bad_dtype, bench))
    probes.append(_expect_error("invalid_layout", Codes.LAYOUT_INVALID, bad_layout, bench))
    probes.append(_expect_error("invalid_block", Codes.BLOCK_INVALID, bad_block, bench))
    probes.append(_expect_error("duplicate_job_id", Codes.JOB_DUPLICATE, duplicate, bench))
    probes.append(_expect_error("revision_regression", Codes.REVISION_REGRESSION, revision_regression, bench))
    probes.append(_expect_error("ledger_version_regression", Codes.LEDGER_VERSION_REGRESSION, ledger_regression, bench))
    probes.append(_expect_error("resource_job_limit", Codes.RESOURCE_LIMIT, resource_limit, bench))
    probes.append(_expect_error("stage_busy", Codes.STAGE_BUSY, stage_busy, bench))

    tolerance_probe = _tolerance_probe(bench)
    probes.append(tolerance_probe["summary"])
    report = {
        "ok": all(probe.get("passed") for probe in probes),
        "probe_count": len(probes),
        "probes": probes,
    }
    atomic_write_json(bench.diagnosis_dir / "probes.json", report)
    return report


def _tolerance_probe(bench: Workbench) -> dict[str, Any]:
    spec = normalize_job(
        {
            "job_id": "probe_tolerance",
            "op_kind": "gemm",
            "matrix_shapes": {"A": [5, 4], "B": [4, 6], "C": [5, 6]},
            "dtype": "float64",
            "block_size": 2,
            "alpha": 1.0,
            "beta": 0.25,
            "seed": 9090,
            "revision": 1,
        }
    )
    from .sampling import deterministic_arrays

    arrays = deterministic_arrays(spec)
    result = tiled_multiply_accumulate(spec, arrays)
    faulty = result["output"].copy()
    faulty[0, 0, 0] = faulty[0, 0, 0] + 1.0
    try:
        verify_against_reference(spec, faulty, arrays)
    except WorkbenchError as exc:
        record = bench.record_error(exc, source="probe:tolerance_exceeded")
        diagnosis = {
            "job_id": "probe_tolerance",
            "injected_fault": "added 1.0 to output index [0,0,0]",
            "log_seq": record.get("seq"),
            **(exc.details or {}),
        }
        atomic_write_json(bench.diagnosis_dir / "TOLERANCE-PROBE.json", diagnosis)
        summary = {
            "name": "tolerance_exceeded_first_deviation",
            "expected_code": Codes.TOLERANCE_EXCEEDED,
            "actual_code": exc.code,
            "passed": exc.code == Codes.TOLERANCE_EXCEEDED,
            "first_deviation": (exc.details or {}).get("first_deviation"),
            "log_seq": record.get("seq"),
        }
        return {"summary": summary, "diagnosis": diagnosis}
    raise AssertionError("tolerance probe did not raise")
