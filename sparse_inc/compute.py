"""Row-local multiplication, incremental recomputation, and deviations."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Iterable

import numpy as np
from scipy import sparse

from .canonical import hash_array_f64, sha256_json
from .case import SampleCase
from .codes import E_RESOURCE_LIMIT
from .config import ComponentConfig
from .errors import raise_business
from .models import IncrementalEvent, RowBlock, RowState


@dataclass(frozen=True)
class BlockComputer:
    case: SampleCase
    b_dense: np.ndarray

    def compute(self, row_states: tuple[RowState, ...], row_start: int, row_end: int) -> np.ndarray:
        indptr = [0]
        indices: list[int] = []
        values: list[float] = []
        for state in row_states[row_start:row_end]:
            if state.active:
                indices.extend(state.col_indices)
                values.extend(state.values)
            indptr.append(len(indices))
        block_matrix = sparse.csr_matrix(
            (
                np.asarray(values, dtype=np.float64),
                np.asarray(indices, dtype=np.int64),
                np.asarray(indptr, dtype=np.int64),
            ),
            shape=(row_end - row_start, self.case.k),
            dtype=np.float64,
        )
        if self.case.operation == "spmm":
            result = block_matrix @ self.b_dense
        else:
            result = (block_matrix @ self.case.b.to_scipy()).toarray()
        return np.ascontiguousarray(result, dtype=np.float64)


def make_computer(case: SampleCase) -> BlockComputer:
    return BlockComputer(case=case, b_dense=case.b.to_dense())


def row_states_from_base(case: SampleCase) -> tuple[RowState, ...]:
    return tuple(RowState.from_csr_row(case.a, row) for row in range(case.m))


def block_ranges(rows: int, block_size: int) -> list[tuple[int, int, str]]:
    ranges: list[tuple[int, int, str]] = []
    for index, start in enumerate(range(0, rows, block_size)):
        end = min(rows, start + block_size)
        ranges.append((start, end, f"blk_{index:04d}"))
    return ranges


def input_digest(
    case: SampleCase,
    row_states: tuple[RowState, ...],
    row_start: int,
    row_end: int,
) -> str:
    return sha256_json(
        {
            "operation": case.operation,
            "shape_mnk": list(case.shape_mnk),
            "csr_b_checksum": case.b.checksum,
            "row_start": row_start,
            "row_end": row_end,
            "rows": [state.input_dict() for state in row_states[row_start:row_end]],
        }
    )


def build_block(
    computer: BlockComputer,
    row_states: tuple[RowState, ...],
    row_start: int,
    row_end: int,
    block_id: str,
) -> RowBlock:
    result = computer.compute(row_states, row_start, row_end)
    result.setflags(write=False)
    last_write_version = max(
        (state.version for state in row_states[row_start:row_end]),
        default=0,
    )
    return RowBlock(
        block_id=block_id,
        row_start=row_start,
        row_end=row_end,
        result=result,
        digest=hash_array_f64(result),
        input_digest=input_digest(computer.case, row_states, row_start, row_end),
        last_write_version=last_write_version,
    )


def initial_blocks(
    computer: BlockComputer,
    row_states: tuple[RowState, ...],
    config: ComponentConfig,
) -> tuple[RowBlock, ...]:
    ranges = block_ranges(computer.case.m, config.row_block_size)
    workers = min(config.max_parallel_intervals, len(ranges))
    if workers <= 1:
        return tuple(build_block(computer, row_states, *item) for item in ranges)
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="sparse-inc-init") as pool:
        futures = [pool.submit(build_block, computer, row_states, *item) for item in ranges]
        return tuple(future.result() for future in futures)


def recompute_blocks(
    *,
    computer: BlockComputer,
    row_states: tuple[RowState, ...],
    previous_blocks: tuple[RowBlock, ...],
    row_start: int,
    row_end: int,
    write_version: int,
    batch_id: str,
    config: ComponentConfig,
    case_id: str,
) -> tuple[tuple[RowBlock, ...], IncrementalEvent]:
    affected = [
        block
        for block in previous_blocks
        if block.row_start < row_end and block.row_end > row_start
    ]
    reused = [block for block in previous_blocks if not (block.row_start < row_end and block.row_end > row_start)]
    degradations: list[str] = []

    if len(affected) > config.max_row_buffers:
        if config.resource_policy == "strict":
            raise_business(
                E_RESOURCE_LIMIT,
                case_id=case_id,
                segment="row_buffers",
                message="affected row-block count exceeds max_row_buffers under strict policy",
                batch_id=batch_id,
                write_version=write_version,
                affected_blocks=len(affected),
                max_row_buffers=config.max_row_buffers,
                resource_policy=config.resource_policy,
            )
        degradations.append("row_buffer_limit_chunked")

    chunks = [
        affected[index : index + config.max_row_buffers]
        for index in range(0, len(affected), config.max_row_buffers)
    ]
    built_by_id: dict[str, RowBlock] = {}
    for chunk in chunks:
        if len(chunk) > config.max_parallel_intervals:
            degradations.append("parallel_interval_limit_capped")
        workers = min(config.max_parallel_intervals, len(chunk))
        if workers <= 1:
            for block in chunk:
                built_by_id[block.block_id] = build_block(
                    computer,
                    row_states,
                    block.row_start,
                    block.row_end,
                    block.block_id,
                )
        else:
            with ThreadPoolExecutor(
                max_workers=workers,
                thread_name_prefix="sparse-inc-recompute",
            ) as pool:
                futures = {
                    block.block_id: pool.submit(
                        build_block,
                        computer,
                        row_states,
                        block.row_start,
                        block.row_end,
                        block.block_id,
                    )
                    for block in chunk
                }
                for block in chunk:
                    built_by_id[block.block_id] = futures[block.block_id].result()

    new_blocks: list[RowBlock] = []
    result_changed = False
    for block in previous_blocks:
        if block.block_id in built_by_id:
            replacement = built_by_id[block.block_id]
            new_blocks.append(replacement)
            result_changed = result_changed or replacement.digest != block.digest
        else:
            new_blocks.append(block)

    recompute_intervals = tuple(block.interval_dict() for block in affected)
    reuse_intervals = tuple(block.interval_dict() for block in reused)
    event = IncrementalEvent(
        batch_id=batch_id,
        write_version=write_version,
        row_interval=(row_start, row_end),
        recompute_intervals=recompute_intervals,
        reuse_intervals=reuse_intervals,
        result_changed=result_changed,
        degradation_categories=tuple(sorted(set(degradations))),
    )
    return tuple(new_blocks), event


def materialize_result(blocks: Iterable[RowBlock], rows: int, cols: int) -> np.ndarray:
    result = np.empty((rows, cols), dtype=np.float64, order="C")
    for block in blocks:
        result[block.row_start : block.row_end, :] = block.result
    result.setflags(write=False)
    return result


def dense_reference(case: SampleCase, row_states: tuple[RowState, ...]) -> np.ndarray:
    a_dense = np.zeros((case.m, case.k), dtype=np.float64)
    for state in row_states:
        if state.active and state.col_indices:
            a_dense[state.row, list(state.col_indices)] = np.asarray(
                state.values,
                dtype=np.float64,
            )
    b_dense = case.b.to_dense()
    return np.ascontiguousarray(a_dense @ b_dense, dtype=np.float64)


def deviation_report(result: np.ndarray, reference: np.ndarray, config: ComponentConfig) -> dict[str, Any]:
    if result.shape != reference.shape:
        raise ValueError("result and reference shapes differ")
    difference = np.abs(result - reference)
    allowed = config.tolerance_abs + config.tolerance_rel * np.abs(reference)
    failed_mask = difference > allowed
    failed = int(np.count_nonzero(failed_mask))
    max_abs = float(np.max(difference)) if difference.size else 0.0
    denominators = np.maximum(np.abs(reference), np.finfo(np.float64).tiny)
    max_rel = float(np.max(difference / denominators)) if difference.size else 0.0
    norm_ref = float(np.linalg.norm(reference))
    norm_diff = float(np.linalg.norm(difference))
    global_rel = norm_diff / norm_ref if norm_ref > 0 else (0.0 if norm_diff == 0 else float("inf"))
    return {
        "reference": "float64_dense_matmul",
        "reference_dtype": "float64",
        "result_dtype": "float64",
        "tolerance": {
            "abs": config.tolerance_abs,
            "rel": config.tolerance_rel,
            "rule": "abs_error <= abs_tol + rel_tol * abs(reference)",
        },
        "max_abs_error": max_abs,
        "max_element_rel_error": max_rel,
        "global_rel_error": global_rel,
        "failed_elements": failed,
        "element_count": int(result.size),
        "within_tolerance": failed == 0 and bool(np.all(np.isfinite(result))),
        "result_checksum": hash_array_f64(result),
        "reference_checksum": hash_array_f64(reference),
    }


def result_summary(result: np.ndarray) -> dict[str, Any]:
    flat = result.reshape(-1)
    return {
        "shape": list(result.shape),
        "dtype": "float64",
        "nnz": int(np.count_nonzero(flat)),
        "element_count": int(flat.size),
        "checksum": hash_array_f64(result),
        "l2_norm": float(np.linalg.norm(flat)),
        "max_abs": float(np.max(np.abs(flat))) if flat.size else 0.0,
        "sample_rows": result[: min(2, result.shape[0])].tolist(),
    }
