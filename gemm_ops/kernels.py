"""GEMM/BMM kernels with tiled loop variants and deterministic scheduling."""

from __future__ import annotations

import hashlib
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import numpy as np

from .config import Tile, canonical_json, parse_tile
from .errors import GemmError


LOOP_ORDERS = frozenset({"ijk", "ikj", "jik"})
BATCH_SCHEDULES = frozenset({"serial", "batch_parallel", "flattened"})


@dataclass
class BmmExecution:
    output: np.ndarray
    statistics: dict[str, Any]
    batch_decisions: list[dict[str, Any]]
    scheduler: dict[str, Any]
    elapsed_seconds: float


def validate_tile(
    tile: Tile | Mapping[str, Any] | Sequence[int],
    *,
    max_tile_elements: int = 1_048_576,
    max_tile_dimension: int = 4096,
) -> Tile:
    if isinstance(tile, Tile):
        parsed = tile
    elif isinstance(tile, Mapping):
        parsed = parse_tile(tile)
    elif isinstance(tile, Sequence) and not isinstance(tile, (str, bytes)):
        if len(tile) != 3:
            raise GemmError(
                "E_TILE_INVALID",
                "tile sequence must have exactly three entries",
                {"tile": list(tile)},
            )
        parsed = parse_tile({"BM": tile[0], "BN": tile[1], "BK": tile[2]})
    else:
        raise GemmError(
            "E_TILE_INVALID",
            "tile must be a Tile, mapping or three-item sequence",
            {"tile_type": type(tile).__name__},
        )
    dimensions = parsed.as_dict()
    for name, value in dimensions.items():
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise GemmError(
                "E_TILE_INVALID",
                f"tile dimension {name} must be a positive integer",
                {"tile": dimensions, "field": name},
            )
        if value > max_tile_dimension:
            raise GemmError(
                "E_TILE_INVALID",
                f"tile dimension {name} exceeds max_tile_dimension",
                {"tile": dimensions, "max_tile_dimension": max_tile_dimension},
            )
    element_count = parsed.BM * parsed.BN * parsed.BK
    if element_count > max_tile_elements:
        raise GemmError(
            "E_TILE_INVALID",
            "tile element count exceeds max_tile_elements",
            {
                "tile": dimensions,
                "tile_elements": element_count,
                "max_tile_elements": max_tile_elements,
            },
        )
    return parsed


def _validate_scalar(name: str, value: Any) -> np.float32:
    array = np.asarray(value)
    if array.ndim != 0:
        raise GemmError(
            "E_SHAPE_MISMATCH",
            f"{name} must be a scalar",
            {"parameter": name, "shape": list(array.shape)},
        )
    try:
        scalar = np.float32(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise GemmError(
            "E_NONFINITE_INPUT",
            f"{name} cannot be represented as float32",
            {"parameter": name, "value": str(value), "error": str(exc)},
        ) from exc
    if not np.isfinite(scalar):
        raise GemmError(
            "E_NONFINITE_INPUT",
            f"{name} is not finite",
            {"parameter": name, "value": float(scalar)},
        )
    return scalar


def _as_float32(array: np.ndarray, label: str) -> np.ndarray:
    result = np.asarray(array)
    if result.dtype != np.dtype(np.float32):
        result = result.astype(np.float32, copy=False)
    if not np.isfinite(result).all():
        count = int(np.count_nonzero(~np.isfinite(result)))
        raise GemmError(
            "E_NONFINITE_INPUT",
            f"matrix {label} contains non-finite elements",
            {"matrix": label, "nonfinite_count": count},
        )
    return result


def _shape_mismatch(message: str, **details: Any) -> GemmError:
    return GemmError("E_SHAPE_MISMATCH", message, details)


def _prepare_gemm(
    A: Any,
    B: Any,
    C: Any,
    alpha: Any,
    beta: Any,
) -> tuple[np.ndarray, np.ndarray, np.ndarray | None, np.float32, np.float32, int, int, int]:
    alpha32 = _validate_scalar("alpha", alpha)
    beta32 = _validate_scalar("beta", beta)
    A_arr = _as_float32(np.asarray(A), "A")
    B_arr = _as_float32(np.asarray(B), "B")
    if A_arr.ndim != 2:
        raise _shape_mismatch("A must be a 2-D matrix", A_shape=list(A_arr.shape))
    if B_arr.ndim != 2:
        raise _shape_mismatch("B must be a 2-D matrix", B_shape=list(B_arr.shape))
    M, K = A_arr.shape
    if B_arr.shape[0] != K:
        raise _shape_mismatch(
            "A and B have incompatible inner dimensions",
            A_shape=list(A_arr.shape),
            B_shape=list(B_arr.shape),
        )
    N = int(B_arr.shape[1])
    C_arr = np.asarray(C)
    if C_arr.shape != (M, N):
        raise _shape_mismatch(
            "C shape must be M by N",
            A_shape=list(A_arr.shape),
            B_shape=list(B_arr.shape),
            C_shape=list(C_arr.shape),
            expected_C_shape=[M, N],
        )
    if beta32 != np.float32(0.0):
        C_arr = _as_float32(C_arr, "C")
    return A_arr, B_arr, C_arr if beta32 != np.float32(0.0) else None, alpha32, beta32, M, N, K


def _prepare_bmm(
    A: Any,
    B: Any,
    C: Any,
    alpha: Any,
    beta: Any,
) -> tuple[
    np.ndarray,
    np.ndarray,
    np.ndarray | None,
    np.float32,
    np.float32,
    int,
    int,
    int,
    int,
]:
    alpha32 = _validate_scalar("alpha", alpha)
    beta32 = _validate_scalar("beta", beta)
    A_arr = _as_float32(np.asarray(A), "A")
    B_arr = _as_float32(np.asarray(B), "B")
    if A_arr.ndim != 3 or B_arr.ndim != 3:
        raise _shape_mismatch(
            "A and B must be 3-D batched matrices",
            A_shape=list(A_arr.shape),
            B_shape=list(B_arr.shape),
        )
    batch, M, K = A_arr.shape
    if B_arr.shape[0] != batch:
        raise _shape_mismatch(
            "A and B batch sizes differ",
            A_shape=list(A_arr.shape),
            B_shape=list(B_arr.shape),
        )
    if B_arr.shape[1] != K:
        raise _shape_mismatch(
            "A and B have incompatible inner dimensions",
            A_shape=list(A_arr.shape),
            B_shape=list(B_arr.shape),
        )
    N = int(B_arr.shape[2])
    C_arr = np.asarray(C)
    if C_arr.ndim != 3 or C_arr.shape != (batch, M, N):
        raise _shape_mismatch(
            "C shape must be batch by M by N",
            A_shape=list(A_arr.shape),
            B_shape=list(B_arr.shape),
            C_shape=list(C_arr.shape),
            expected_C_shape=[batch, M, N],
        )
    if beta32 != np.float32(0.0):
        C_arr = _as_float32(C_arr, "C")
    return (
        A_arr,
        B_arr,
        C_arr if beta32 != np.float32(0.0) else None,
        alpha32,
        beta32,
        batch,
        M,
        N,
        K,
    )


def _epilogue(
    accumulator: np.ndarray,
    alpha: np.float32,
    beta: np.float32,
    c_tile: np.ndarray | None,
    output_tile: np.ndarray,
) -> None:
    scaled = np.multiply(accumulator, alpha, dtype=np.float32)
    if beta != np.float32(0.0):
        if c_tile is None:
            raise RuntimeError("C tile is required when beta is non-zero")
        scaled += np.multiply(c_tile, beta, dtype=np.float32)
    output_tile[...] = scaled


def _validate_loop_order(loop_order: str) -> str:
    if loop_order not in LOOP_ORDERS:
        raise GemmError(
            "E_TILE_INVALID",
            "loop_order must be one of ijk, ikj, jik",
            {"loop_order": loop_order},
        )
    return loop_order


def _basic_gemm_validated(
    A: np.ndarray,
    B: np.ndarray,
    C: np.ndarray | None,
    alpha: np.float32,
    beta: np.float32,
) -> np.ndarray:
    product = np.matmul(A, B).astype(np.float32, copy=False)
    scaled = np.multiply(product, alpha, dtype=np.float32)
    if beta != np.float32(0.0):
        if C is None:
            raise RuntimeError("C is required when beta is non-zero")
        scaled += np.multiply(C, beta, dtype=np.float32)
    return scaled.astype(np.float32, copy=False)


def _tiled_gemm_validated(
    A: np.ndarray,
    B: np.ndarray,
    C: np.ndarray | None,
    alpha: np.float32,
    beta: np.float32,
    tile: Tile,
    loop_order: str,
) -> np.ndarray:
    M, K = A.shape
    N = B.shape[1]
    BM, BN, BK = tile.BM, tile.BN, tile.BK
    output = np.empty((M, N), dtype=np.float32)
    order = _validate_loop_order(loop_order)

    if order == "ijk":
        for i0 in range(0, M, BM):
            i1 = min(i0 + BM, M)
            for j0 in range(0, N, BN):
                j1 = min(j0 + BN, N)
                accumulator = np.zeros((i1 - i0, j1 - j0), dtype=np.float32)
                for k0 in range(0, K, BK):
                    k1 = min(k0 + BK, K)
                    accumulator += A[i0:i1, k0:k1] @ B[k0:k1, j0:j1]
                c_tile = None if C is None else C[i0:i1, j0:j1]
                _epilogue(accumulator, alpha, beta, c_tile, output[i0:i1, j0:j1])
        return output

    if order == "ikj":
        for i0 in range(0, M, BM):
            i1 = min(i0 + BM, M)
            accumulator = np.zeros((i1 - i0, N), dtype=np.float32)
            for k0 in range(0, K, BK):
                k1 = min(k0 + BK, K)
                for j0 in range(0, N, BN):
                    j1 = min(j0 + BN, N)
                    accumulator[:, j0:j1] += A[i0:i1, k0:k1] @ B[k0:k1, j0:j1]
            c_tile = None if C is None else C[i0:i1, :]
            _epilogue(accumulator, alpha, beta, c_tile, output[i0:i1, :])
        return output

    for j0 in range(0, N, BN):
        j1 = min(j0 + BN, N)
        accumulator = np.zeros((M, j1 - j0), dtype=np.float32)
        for i0 in range(0, M, BM):
            i1 = min(i0 + BM, M)
            for k0 in range(0, K, BK):
                k1 = min(k0 + BK, K)
                accumulator[i0:i1, :] += A[i0:i1, k0:k1] @ B[k0:k1, j0:j1]
        c_tile = None if C is None else C[:, j0:j1]
        _epilogue(accumulator, alpha, beta, c_tile, output[:, j0:j1])
    return output


def gemm(
    A: Any,
    B: Any,
    C: Any,
    alpha: Any = 1.0,
    beta: Any = 0.0,
    *,
    tile: Tile | Mapping[str, Any] | Sequence[int] | None = None,
    loop_order: str = "ijk",
    max_tile_elements: int = 1_048_576,
    max_tile_dimension: int = 4096,
) -> np.ndarray:
    """Compute float32 C = alpha*A*B + beta*C."""

    A_arr, B_arr, C_arr, alpha32, beta32, _, _, _ = _prepare_gemm(A, B, C, alpha, beta)
    if tile is None:
        return _basic_gemm_validated(A_arr, B_arr, C_arr, alpha32, beta32)
    parsed_tile = validate_tile(
        tile,
        max_tile_elements=max_tile_elements,
        max_tile_dimension=max_tile_dimension,
    )
    return _tiled_gemm_validated(
        A_arr, B_arr, C_arr, alpha32, beta32, parsed_tile, loop_order
    )


def gemm_basic(
    A: Any,
    B: Any,
    C: Any,
    alpha: Any = 1.0,
    beta: Any = 0.0,
) -> np.ndarray:
    return gemm(A, B, C, alpha, beta, tile=None)


def gemm_tiled(
    A: Any,
    B: Any,
    C: Any,
    alpha: Any,
    beta: Any,
    tile: Tile | Mapping[str, Any] | Sequence[int],
    loop_order: str,
    *,
    max_tile_elements: int = 1_048_576,
    max_tile_dimension: int = 4096,
) -> np.ndarray:
    return gemm(
        A,
        B,
        C,
        alpha,
        beta,
        tile=tile,
        loop_order=loop_order,
        max_tile_elements=max_tile_elements,
        max_tile_dimension=max_tile_dimension,
    )


def _batch_sort_key(index: int) -> str:
    return f"batch:{index:08d}"


def _output_checksum(output: np.ndarray) -> str:
    contiguous = np.ascontiguousarray(output, dtype=np.float32)
    digest = hashlib.sha256()
    digest.update(str(contiguous.shape).encode("ascii"))
    digest.update(contiguous.tobytes(order="C"))
    return digest.hexdigest()


def _make_statistics(
    output: np.ndarray,
    batch: int,
    strategy_id: str,
    tile: Tile | None,
    loop_order: str,
    batch_decisions: list[dict[str, Any]],
) -> dict[str, Any]:
    keys = [item["task_sort_key"] for item in batch_decisions]
    ordered_indices = hashlib.sha256(
        ",".join(str(item["batch_index"]) for item in batch_decisions).encode("ascii")
    ).hexdigest()
    return {
        "batch_count": batch,
        "output_element_count": int(output.size),
        "strategy_id": strategy_id,
        "tile": None if tile is None else tile.as_dict(),
        "loop_order": loop_order,
        "sort_key_min": min(keys),
        "sort_key_max": max(keys),
        "ordered_batch_indices_sha256": ordered_indices,
        "batch_decisions_sha256": hashlib.sha256(
            canonical_json(batch_decisions).encode("utf-8")
        ).hexdigest(),
        "output_checksum_sha256": _output_checksum(output),
    }


def execute_bmm(
    A: Any,
    B: Any,
    C: Any,
    alpha: Any = 1.0,
    beta: Any = 0.0,
    *,
    tile: Tile | Mapping[str, Any] | Sequence[int] | None,
    loop_order: str,
    batch_schedule: str,
    max_workers: int,
    max_tile_elements: int = 1_048_576,
    max_tile_dimension: int = 4096,
    flatten_chunk_size: int = 16,
    strategy_id: str = "inline",
) -> BmmExecution:
    if batch_schedule not in BATCH_SCHEDULES:
        raise GemmError(
            "E_CONFIG_INVALID",
            "batch_schedule must be serial, batch_parallel or flattened",
            {"batch_schedule": batch_schedule},
        )
    if isinstance(max_workers, bool) or not isinstance(max_workers, int) or max_workers <= 0:
        raise GemmError(
            "E_CONFIG_INVALID",
            "max_workers must be a positive integer",
            {"max_workers": max_workers},
        )
    if isinstance(flatten_chunk_size, bool) or not isinstance(flatten_chunk_size, int) or flatten_chunk_size <= 0:
        raise GemmError(
            "E_CONFIG_INVALID",
            "flatten_chunk_size must be a positive integer",
            {"flatten_chunk_size": flatten_chunk_size},
        )
    parsed_tile = None
    if tile is not None:
        parsed_tile = validate_tile(
            tile,
            max_tile_elements=max_tile_elements,
            max_tile_dimension=max_tile_dimension,
        )
    order = "numpy_matmul" if parsed_tile is None else _validate_loop_order(loop_order)
    A_arr, B_arr, C_arr, alpha32, beta32, batch, M, N, K = _prepare_bmm(
        A, B, C, alpha, beta
    )
    output = np.empty((batch, M, N), dtype=np.float32)

    def compute_batch(index: int) -> None:
        if parsed_tile is None:
            output[index] = _basic_gemm_validated(
                A_arr[index], B_arr[index], C_arr[index] if C_arr is not None else None,
                alpha32, beta32
            )
        else:
            output[index] = _tiled_gemm_validated(
                A_arr[index],
                B_arr[index],
                C_arr[index] if C_arr is not None else None,
                alpha32,
                beta32,
                parsed_tile,
                order,
            )

    start_time = time.perf_counter()
    effective_schedule = batch_schedule
    if batch == 1:
        compute_batch(0)
        effective_schedule = "serial"
    elif batch_schedule == "serial":
        for batch_index in range(batch):
            compute_batch(batch_index)
    elif batch_schedule == "batch_parallel":
        with ThreadPoolExecutor(max_workers=min(max_workers, batch)) as pool:
            futures = [pool.submit(compute_batch, batch_index) for batch_index in range(batch)]
            for future in futures:
                future.result()
    else:
        chunks = [
            (start, min(start + flatten_chunk_size, batch))
            for start in range(0, batch, flatten_chunk_size)
        ]
        with ThreadPoolExecutor(max_workers=min(max_workers, len(chunks))) as pool:
            futures = []
            for start, end in chunks:
                def compute_chunk(chunk_start: int = start, chunk_end: int = end) -> None:
                    for batch_index in range(chunk_start, chunk_end):
                        compute_batch(batch_index)

                futures.append(pool.submit(compute_chunk))
            for future in futures:
                future.result()
    elapsed = time.perf_counter() - start_time

    batch_decisions: list[dict[str, Any]] = []
    for batch_index in range(batch):
        batch_decisions.append(
            {
                "batch_index": batch_index,
                "task_sort_key": _batch_sort_key(batch_index),
                "strategy_id": strategy_id,
                "tile": None if parsed_tile is None else parsed_tile.as_dict(),
                "loop_order": order,
            }
        )
    statistics = _make_statistics(
        output,
        batch,
        strategy_id,
        parsed_tile,
        order,
        batch_decisions,
    )
    chunk_records = []
    if effective_schedule == "flattened":
        for chunk_start in range(0, batch, flatten_chunk_size):
            chunk_end = min(chunk_start + flatten_chunk_size, batch)
            chunk_records.append(
                {
                    "chunk_sort_key": f"chunk:{chunk_start:08d}:{chunk_end - 1:08d}",
                    "batch_indices": list(range(chunk_start, chunk_end)),
                }
            )
    scheduler = {
        "requested_batch_schedule": batch_schedule,
        "effective_batch_schedule": effective_schedule,
        "max_workers_requested": max_workers,
        "flatten_chunk_size": flatten_chunk_size,
        "task_count": batch
        if effective_schedule in {"serial", "batch_parallel"}
        else len(chunk_records),
        "chunk_records": chunk_records,
    }
    return BmmExecution(
        output=output,
        statistics=statistics,
        batch_decisions=batch_decisions,
        scheduler=scheduler,
        elapsed_seconds=elapsed,
    )


def bmm(
    A: Any,
    B: Any,
    C: Any,
    alpha: Any = 1.0,
    beta: Any = 0.0,
    *,
    tile: Tile | Mapping[str, Any] | Sequence[int] | None = None,
    loop_order: str = "ijk",
    batch_schedule: str = "serial",
    max_workers: int = 1,
    max_tile_elements: int = 1_048_576,
    max_tile_dimension: int = 4096,
    flatten_chunk_size: int = 16,
) -> np.ndarray:
    return execute_bmm(
        A,
        B,
        C,
        alpha,
        beta,
        tile=tile,
        loop_order=loop_order,
        batch_schedule=batch_schedule,
        max_workers=max_workers,
        max_tile_elements=max_tile_elements,
        max_tile_dimension=max_tile_dimension,
        flatten_chunk_size=flatten_chunk_size,
    ).output


def _prepare_reference(
    A: Any,
    B: Any,
    C: Any,
    alpha: Any,
    beta: Any,
    *,
    batched: bool,
) -> tuple[np.ndarray, np.ndarray, np.ndarray | None, np.float64, np.float64, tuple[int, ...]]:
    try:
        alpha64 = np.float64(alpha)
        beta64 = np.float64(beta)
    except (TypeError, ValueError, OverflowError) as exc:
        raise GemmError(
            "E_NONFINITE_INPUT",
            "alpha or beta cannot be represented as float64",
            {"error": str(exc)},
        ) from exc
    if not np.isfinite(alpha64) or not np.isfinite(beta64):
        raise GemmError(
            "E_NONFINITE_INPUT",
            "alpha or beta is not finite",
            {"alpha": float(alpha64), "beta": float(beta64)},
        )
    A_arr = np.asarray(A, dtype=np.float64)
    B_arr = np.asarray(B, dtype=np.float64)
    expected_ndim = 3 if batched else 2
    if A_arr.ndim != expected_ndim or B_arr.ndim != expected_ndim:
        raise _shape_mismatch(
            "reference inputs have an unexpected rank",
            A_shape=list(A_arr.shape),
            B_shape=list(B_arr.shape),
        )
    if not np.isfinite(A_arr).all() or not np.isfinite(B_arr).all():
        raise GemmError("E_NONFINITE_INPUT", "reference A or B contains non-finite values")
    if batched:
        batch, M, K = A_arr.shape
        if B_arr.shape[0] != batch or B_arr.shape[1] != K:
            raise _shape_mismatch(
                "reference batched shapes are incompatible",
                A_shape=list(A_arr.shape),
                B_shape=list(B_arr.shape),
            )
        N = int(B_arr.shape[2])
        expected_c = (batch, M, N)
    else:
        M, K = A_arr.shape
        if B_arr.shape[0] != K:
            raise _shape_mismatch(
                "reference matrix shapes are incompatible",
                A_shape=list(A_arr.shape),
                B_shape=list(B_arr.shape),
            )
        N = int(B_arr.shape[1])
        expected_c = (M, N)
    C_arr = np.asarray(C)
    if C_arr.shape != expected_c:
        raise _shape_mismatch(
            "reference C has an unexpected shape",
            C_shape=list(C_arr.shape),
            expected_C_shape=list(expected_c),
        )
    C64: np.ndarray | None = None
    if beta64 != 0.0:
        C64 = np.asarray(C, dtype=np.float64)
        if not np.isfinite(C64).all():
            raise GemmError("E_NONFINITE_INPUT", "reference C contains non-finite values")
    return A_arr, B_arr, C64, alpha64, beta64, expected_c


def gemm_reference(
    A: Any,
    B: Any,
    C: Any,
    alpha: Any = 1.0,
    beta: Any = 0.0,
) -> np.ndarray:
    A64, B64, C64, alpha64, beta64, _ = _prepare_reference(
        A, B, C, alpha, beta, batched=False
    )
    result = np.matmul(A64, B64) * alpha64
    if beta64 != 0.0:
        if C64 is None:
            raise RuntimeError("C is required when beta is non-zero")
        result += beta64 * C64
    return result.astype(np.float64, copy=False)


def bmm_reference(
    A: Any,
    B: Any,
    C: Any,
    alpha: Any = 1.0,
    beta: Any = 0.0,
) -> np.ndarray:
    A64, B64, C64, alpha64, beta64, shape = _prepare_reference(
        A, B, C, alpha, beta, batched=True
    )
    output = np.empty(shape, dtype=np.float64)
    for batch_index in range(shape[0]):
        output[batch_index] = np.matmul(A64[batch_index], B64[batch_index]) * alpha64
        if beta64 != 0.0:
            if C64 is None:
                raise RuntimeError("C is required when beta is non-zero")
            output[batch_index] += beta64 * C64[batch_index]
    return output
