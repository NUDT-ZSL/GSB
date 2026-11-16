"""Threaded row-block execution, atomic checkpoints and deterministic resume."""

from __future__ import annotations

import json
import os
import threading
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import numpy as np

from .attribution import PrecisionSettings, attribute_row, precision_settings
from .csr import CSRMatrix, generate_csr
from .dense import generate_b, generate_c, validate_finite, validate_shapes
from .errors import ConfigError, ResumeError
from .kernels import (
    VALID_ORDERS,
    chunked_row,
    reference_row,
    validate_block_size,
)
from .reporting import assemble_report
from .strategy import select_strategy
from .utils import atomic_write_json, canonical_json_bytes, sha256_hex


@dataclass
class CaseInputs:
    sample_config: dict[str, Any]
    strategy_config: dict[str, Any]
    csr: CSRMatrix
    csr_metadata: dict[str, Any]
    B: np.ndarray
    C: np.ndarray
    precision: PrecisionSettings
    strategy: dict[str, Any]
    case_digest: str


def prepare_case(
    sample_config: dict[str, Any], strategy_config: dict[str, Any]
) -> CaseInputs:
    precision = precision_settings(sample_config)
    csr, csr_metadata = generate_csr(sample_config)
    B = generate_b(sample_config, K=csr.cols, N=int(sample_config["shape"]["N"]))
    C = generate_c(sample_config, M=csr.rows, N=int(B.shape[1]))
    validate_shapes(csr, B, C, stage="baseline-shape-validation")
    validate_finite(csr, B, C, stage="baseline-finite-validation")
    strategy = select_strategy(csr, B, sample_config, strategy_config)
    validate_block_size(strategy["block_size"], K=csr.cols, stage="selected-strategy-validation")
    digest = sha256_hex(
        canonical_json_bytes({"sample": sample_config, "strategy_table": strategy_config})
    )
    return CaseInputs(
        sample_config=sample_config,
        strategy_config=strategy_config,
        csr=csr,
        csr_metadata=csr_metadata,
        B=B,
        C=C,
        precision=precision,
        strategy=strategy,
        case_digest=digest,
    )


def _block_definitions(rows: int, row_batch_size: int) -> list[tuple[int, int, str]]:
    return [
        (start, min(start + row_batch_size, rows), f"RB-{start:06d}-{min(start + row_batch_size, rows):06d}")
        for start in range(0, rows, row_batch_size)
    ]


def _compute_row_block(case: CaseInputs, start: int, end: int, block_key: str) -> dict[str, Any]:
    selected_f32_rows: list[list[float]] = []
    reference_f32_rows: list[list[float]] = []
    reference_f64_rows: list[list[float]] = []
    selected_f64_rows: list[list[float]] = []
    selected_f32_records: list[dict[str, Any]] = []
    reference_f32_records: list[dict[str, Any]] = []
    reference_f64_records: list[dict[str, Any]] = []
    selected_f64_records: list[dict[str, Any]] = []
    elements: list[dict[str, Any]] = []
    variant_rows: dict[str, list[list[float]]] = {order: [] for order in VALID_ORDERS}
    variant_records: dict[str, list[dict[str, Any]]] = {order: [] for order in VALID_ORDERS}
    alpha = float(case.sample_config.get("alpha", 1.0))
    beta = float(case.sample_config.get("beta", 0.0))
    block_size = int(case.strategy["block_size"])
    selected_order = str(case.strategy["order"])

    for row in range(start, end):
        selected32 = chunked_row(
            case.csr,
            case.B,
            case.C,
            row=row,
            alpha=alpha,
            beta=beta,
            block_size=block_size,
            order=selected_order,
            dtype=np.float32,
            trace=True,
        )
        reference32 = reference_row(
            case.csr,
            case.B,
            case.C,
            row=row,
            alpha=alpha,
            beta=beta,
            dtype=np.float32,
        )
        reference64 = reference_row(
            case.csr,
            case.B,
            case.C,
            row=row,
            alpha=alpha,
            beta=beta,
            dtype=np.float64,
        )
        selected64 = chunked_row(
            case.csr,
            case.B,
            case.C,
            row=row,
            alpha=alpha,
            beta=beta,
            block_size=block_size,
            order=selected_order,
            dtype=np.float64,
            trace=False,
        )
        selected_f32_rows.append(selected32.values.tolist())
        reference_f32_rows.append(reference32.values.tolist())
        reference_f64_rows.append(reference64.values.tolist())
        selected_f64_rows.append(selected64.values.tolist())
        selected_f32_records.append(selected32.record)
        reference_f32_records.append(reference32.record)
        reference_f64_records.append(reference64.record)
        selected_f64_records.append(selected64.record)
        elements.extend(
            attribute_row(
                case.csr,
                case.B,
                result=selected32,
                reference64=reference64.values.astype(np.float64),
                alpha=alpha,
                settings=case.precision,
            )
        )
        for order in VALID_ORDERS:
            if order == selected_order:
                variant_result = selected32
            else:
                variant_result = chunked_row(
                    case.csr,
                    case.B,
                    case.C,
                    row=row,
                    alpha=alpha,
                    beta=beta,
                    block_size=block_size,
                    order=order,
                    dtype=np.float32,
                    trace=False,
                )
            variant_rows[order].append(variant_result.values.tolist())
            variant_records[order].append(variant_result.record)

    result: dict[str, Any] = {
        "key": block_key,
        "start_row": int(start),
        "end_row": int(end),
        "row_count": int(end - start),
        "selected_f32_rows": selected_f32_rows,
        "reference_f32_rows": reference_f32_rows,
        "reference_f64_rows": reference_f64_rows,
        "selected_f64_rows": selected_f64_rows,
        "selected_f32_row_records": selected_f32_records,
        "reference_f32_row_records": reference_f32_records,
        "reference_f64_row_records": reference_f64_records,
        "selected_f64_row_records": selected_f64_records,
        "elements": elements,
    }
    for order in VALID_ORDERS:
        result[f"variant_f32_rows_{order}"] = variant_rows[order]
        result[f"variant_f32_row_records_{order}"] = variant_records[order]
    return result


def _progress_identity(case: CaseInputs, row_batch_size: int) -> dict[str, Any]:
    return {
        "sample_id": case.sample_config.get("sample_id", "unnamed"),
        "structure_fingerprint": case.csr_metadata["structure_fingerprint"],
        "strategy_signature": case.strategy["strategy_signature"],
        "case_digest": case.case_digest,
        "row_batch_size": int(row_batch_size),
        "block_keys": [key for _, _, key in _block_definitions(case.csr.rows, row_batch_size)],
    }


def _checkpoint_checksum(identity: dict[str, Any], completed: dict[str, Any]) -> str:
    payload = {"identity": identity, "completed_blocks": completed}
    return sha256_hex(canonical_json_bytes(payload))


def _write_progress(
    path: str | Path,
    *,
    case: CaseInputs,
    identity: dict[str, Any],
    completed: dict[str, Any],
    status: str,
    exit_code: int | None,
    stop_reason: str | None,
) -> dict[str, Any]:
    payload = {
        "schema_version": 1,
        "status": status,
        "identity": identity,
        "completed_block_keys": sorted(completed),
        "completed_row_count": sum(int(block["row_count"]) for block in completed.values()),
        "completed_blocks": completed,
        "checkpoint_sha256": _checkpoint_checksum(identity, completed),
        "exit_code": exit_code,
        "stop_reason": stop_reason,
    }
    atomic_write_json(path, payload)
    return payload


def _load_progress(
    path: str | Path,
    *,
    identity: dict[str, Any],
) -> dict[str, Any] | None:
    progress_path = Path(path)
    if not progress_path.exists():
        return None
    try:
        with progress_path.open("r", encoding="utf-8") as stream:
            payload = json.load(stream)
    except (OSError, json.JSONDecodeError) as exc:
        raise ResumeError(
            f"cannot read checkpoint: {exc}",
            stage="resume-load",
            details={"path": str(progress_path)},
        ) from exc
    if payload.get("schema_version") != 1:
        raise ResumeError("unsupported checkpoint schema", stage="resume-load")
    if payload.get("identity") != identity:
        raise ResumeError(
            "checkpoint identity does not match the current sample/strategy",
            stage="resume-load",
            details={"checkpoint_identity": payload.get("identity"), "current_identity": identity},
        )
    if payload.get("status") not in {"RUNNING", "INTERRUPTED"}:
        return None
    completed = payload.get("completed_blocks", {})
    if not isinstance(completed, dict):
        raise ResumeError("checkpoint completed_blocks must be an object", stage="resume-load")
    expected = set(identity["block_keys"])
    if not set(completed).issubset(expected):
        raise ResumeError("checkpoint contains unknown row blocks", stage="resume-load")
    if payload.get("completed_block_keys") != sorted(completed):
        raise ResumeError("checkpoint block key list is inconsistent", stage="resume-load")
    checksum = _checkpoint_checksum(identity, completed)
    if payload.get("checkpoint_sha256") != checksum:
        raise ResumeError("checkpoint checksum mismatch", stage="resume-load")
    return payload


@dataclass
class RunOutcome:
    status: str
    exit_code: int
    report: dict[str, Any] | None
    progress: dict[str, Any]
    report_path: str
    progress_path: str


def run_case(
    case: CaseInputs,
    *,
    report_path: str | Path,
    progress_path: str | Path,
    max_workers: int = 4,
    fresh: bool = False,
    stop_event: threading.Event | None = None,
    interrupt_after_blocks: int | None = None,
    external_stop_reason: str | Callable[[], str | None] | None = None,
) -> RunOutcome:
    if max_workers <= 0:
        raise ConfigError("max_workers must be positive", stage="execution-config")
    row_batch_size = int(case.sample_config.get("execution", {}).get("row_batch_size", 8))
    if row_batch_size <= 0:
        raise ConfigError("execution.row_batch_size must be positive", stage="execution-config")
    if interrupt_after_blocks is not None and interrupt_after_blocks <= 0:
        raise ConfigError("interrupt_after_blocks must be positive", stage="execution-config")
    event = stop_event if stop_event is not None else threading.Event()
    def resolve_external_reason() -> str | None:
        if callable(external_stop_reason):
            return external_stop_reason()
        return external_stop_reason

    report_file = Path(report_path)
    progress_file = Path(progress_path)
    if fresh:
        progress_file.unlink(missing_ok=True)
        report_file.unlink(missing_ok=True)

    blocks = _block_definitions(case.csr.rows, row_batch_size)
    identity = _progress_identity(case, row_batch_size)
    existing = None if fresh else _load_progress(progress_file, identity=identity)
    completed: dict[str, Any] = dict(existing["completed_blocks"]) if existing else {}
    resumed_blocks = len(completed)
    initial_status = "RUNNING"
    _write_progress(
        progress_file,
        case=case,
        identity=identity,
        completed=completed,
        status=initial_status,
        exit_code=None,
        stop_reason=None,
    )

    missing = [block for block in blocks if block[2] not in completed]
    completed_this_run = 0
    stop_reason: str | None = None
    if event.is_set():
        stop_reason = resolve_external_reason() or "stop-event-before-start"
    elif interrupt_after_blocks is not None and completed_this_run >= interrupt_after_blocks:
        stop_reason = "simulated-interrupt"
        event.set()

    executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="spmm-rowblock")
    pending: dict[Future[dict[str, Any]], tuple[int, int, str]] = {}
    missing_index = 0
    try:
        while True:
            if not event.is_set():
                while len(pending) < max_workers and missing_index < len(missing):
                    start, end, key = missing[missing_index]
                    missing_index += 1
                    future = executor.submit(_compute_row_block, case, start, end, key)
                    pending[future] = (start, end, key)
            if not pending:
                break
            done, _ = wait(tuple(pending), return_when=FIRST_COMPLETED)
            for future in done:
                start, end, key = pending.pop(future)
                completed[key] = future.result()
                completed_this_run += 1
                _write_progress(
                    progress_file,
                    case=case,
                    identity=identity,
                    completed=completed,
                    status="RUNNING",
                    exit_code=None,
                    stop_reason=None,
                )
                if (
                    interrupt_after_blocks is not None
                    and completed_this_run >= interrupt_after_blocks
                    and not event.is_set()
                ):
                    stop_reason = "simulated-interrupt"
                    event.set()
            if event.is_set() and stop_reason is None:
                stop_reason = resolve_external_reason() or "stop-requested"
    finally:
        executor.shutdown(wait=True)

    if event.is_set():
        interrupted = _write_progress(
            progress_file,
            case=case,
            identity=identity,
            completed=completed,
            status="INTERRUPTED",
            exit_code=0,
            stop_reason=stop_reason,
        )
        return RunOutcome(
            status="INTERRUPTED",
            exit_code=0,
            report=None,
            progress=interrupted,
            report_path=str(report_file),
            progress_path=str(progress_file),
        )

    if len(completed) != len(blocks):
        raise ResumeError(
            "execution ended without all row blocks",
            stage="execution-completeness",
            details={"completed": len(completed), "expected": len(blocks)},
        )
    recovery = {
        "mode": "resume" if resumed_blocks else "fresh",
        "resumed_block_count": resumed_blocks,
        "computed_block_count": len(blocks) - resumed_blocks,
        "total_block_count": len(blocks),
        "row_batch_size": row_batch_size,
    }
    report = assemble_report(
        sample_config=case.sample_config,
        strategy_config=case.strategy_config,
        case_digest=case.case_digest,
        csr=case.csr,
        csr_metadata=case.csr_metadata,
        strategy_decision=case.strategy,
        completed=completed,
        blocks=blocks,
        precision=case.precision,
        recovery=recovery,
    )
    atomic_write_json(report_file, report)
    final_progress = _write_progress(
        progress_file,
        case=case,
        identity=identity,
        completed=completed,
        status="COMPLETE",
        exit_code=0,
        stop_reason=None,
    )
    return RunOutcome(
        status="COMPLETE",
        exit_code=0,
        report=report,
        progress=final_progress,
        report_path=str(report_file),
        progress_path=str(progress_file),
    )
