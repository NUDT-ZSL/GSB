"""Three exact TopK routes with one tie and ordering contract."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

import numpy as np

from .errors import fail
from .metrics import ceil_log2, effective_chunk_size
from .types import ROUTES, normalize_route, normalize_tie_policy


@dataclass
class TopKResult:
    route: str
    indices: np.ndarray
    scores: np.ndarray
    comparison_estimate: int
    comparison_observed: int
    tie_policy: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "route": self.route,
            "tie_policy": self.tie_policy,
            "indices": self.indices.astype(np.int64, copy=False).tolist(),
            "scores": self.scores.astype(np.float64, copy=False).tolist(),
            "scores_shape": list(self.scores.shape),
            "comparison_estimate": int(self.comparison_estimate),
            "comparison_observed": int(self.comparison_observed),
            "metadata": dict(self.metadata),
        }


class _HeapSelector:
    """Min-heap of the best k ``(score, -index)`` keys."""

    def __init__(self, k: int) -> None:
        self.k = k
        self.data: list[tuple[float, int, int]] = []
        self.comparisons = 0

    def _less(self, left: tuple[float, int, int], right: tuple[float, int, int]) -> bool:
        self.comparisons += 1
        return left < right

    def _sift_up(self, position: int) -> None:
        while position > 0:
            parent = (position - 1) // 2
            if not self._less(self.data[position], self.data[parent]):
                break
            self.data[position], self.data[parent] = self.data[parent], self.data[position]
            position = parent

    def _sift_down(self, position: int) -> None:
        size = len(self.data)
        while True:
            left = position * 2 + 1
            right = left + 1
            smallest = position
            if left < size and self._less(self.data[left], self.data[smallest]):
                smallest = left
            if right < size and self._less(self.data[right], self.data[smallest]):
                smallest = right
            if smallest == position:
                return
            self.data[position], self.data[smallest] = self.data[smallest], self.data[position]
            position = smallest

    def push(self, item: tuple[float, int, int]) -> None:
        self.data.append(item)
        self._sift_up(len(self.data) - 1)

    def replace_root(self, item: tuple[float, int, int]) -> None:
        self.data[0] = item
        self._sift_down(0)

    def consider(self, item: tuple[float, int, int]) -> None:
        if len(self.data) < self.k:
            self.push(item)
            return
        # Count the root comparison explicitly.
        self.comparisons += 1
        if item > self.data[0]:
            self.replace_root(item)

    def sorted_desc(self) -> list[tuple[float, int, int]]:
        self.comparisons += max(0, len(self.data) * ceil_log2(max(len(self.data), 2)))
        return sorted(self.data, key=lambda item: (-item[0], item[2]))


def _as_float_rows(logits: Any, *, case_id: str | None) -> np.ndarray:
    array = np.asarray(logits)
    if array.ndim == 1:
        array = array.reshape(1, -1)
    if array.ndim != 2:
        fail(
            "E_SHAPE_INVALID",
            "logits must be a 1-D row or a 2-D batch of rows",
            field="logits",
            case_id=case_id,
            details={"shape": list(array.shape)},
        )
    if not np.issubdtype(array.dtype, np.floating):
        fail(
            "E_DTYPE_UNSUPPORTED",
            "logits must use a supported floating-point dtype",
            field="dtype",
            case_id=case_id,
            details={"actual": str(array.dtype)},
        )
    if array.shape[1] <= 0:
        fail(
            "E_SHAPE_INVALID",
            "logits must contain at least one vocabulary element per row",
            field="vocab_size",
            case_id=case_id,
            details={"shape": list(array.shape)},
        )
    if not np.all(np.isfinite(array)):
        bad = np.argwhere(~np.isfinite(array))
        row, position = (int(bad[0, 0]), int(bad[0, 1]))
        fail(
            "E_LOGITS_NON_FINITE",
            "logits contain a non-finite value",
            field="logits",
            case_id=case_id,
            details={
                "first_bad_element": {"row": row, "vocabulary_index": position},
                "value": str(array[row, position]),
            },
        )
    return array


def validate_k(k_value: Any, vocab_size: int, *, case_id: str | None) -> int:
    if isinstance(k_value, bool):
        fail("E_K_INVALID", "k_value must be a positive integer", field="k_value", case_id=case_id)
    try:
        k = int(k_value)
    except (TypeError, ValueError, OverflowError):
        fail("E_K_INVALID", "k_value must be a positive integer", field="k_value", case_id=case_id)
    if isinstance(k_value, (float, np.floating)) and not float(k_value).is_integer():
        fail("E_K_INVALID", "k_value must be an integer", field="k_value", case_id=case_id)
    if k <= 0:
        fail("E_K_INVALID", "k_value must be positive", field="k_value", case_id=case_id)
    if k > vocab_size:
        fail(
            "E_K_INVALID",
            "k_value exceeds vocab_size",
            field="k_value",
            case_id=case_id,
            details={"k_value": k, "vocab_size": int(vocab_size)},
        )
    return k


def _result_from_rows(
    route: str,
    rows_indices: list[np.ndarray],
    rows_scores: list[np.ndarray],
    *,
    comparison_estimate: int,
    comparison_observed: int,
    tie_policy: str,
    metadata: dict[str, Any],
) -> TopKResult:
    return TopKResult(
        route=route,
        indices=np.stack(rows_indices, axis=0).astype(np.int64, copy=False),
        scores=np.stack(rows_scores, axis=0),
        comparison_estimate=int(comparison_estimate),
        comparison_observed=int(comparison_observed),
        tie_policy=tie_policy,
        metadata=metadata,
    )


def _full_sort_rows(rows: np.ndarray, k: int) -> tuple[list[np.ndarray], list[np.ndarray], int]:
    indices_out: list[np.ndarray] = []
    scores_out: list[np.ndarray] = []
    observed = 0
    for row in rows:
        observed += int(row.size * ceil_log2(max(row.size, 2)))
        order = np.argsort(-row, kind="stable")[:k]
        indices_out.append(order.astype(np.int64, copy=False))
        scores_out.append(row[order])
    return indices_out, scores_out, observed


def _heap_select_rows(
    rows: np.ndarray,
    k: int,
) -> tuple[list[np.ndarray], list[np.ndarray], int]:
    indices_out: list[np.ndarray] = []
    scores_out: list[np.ndarray] = []
    observed = 0
    for row in rows:
        selector = _HeapSelector(k)
        for index, value in enumerate(row.tolist()):
            score = float(value)
            selector.consider((score, -index, index))
        selected = selector.sorted_desc()
        indices_out.append(np.asarray([item[2] for item in selected], dtype=np.int64))
        scores_out.append(np.asarray([item[0] for item in selected], dtype=row.dtype))
        observed += selector.comparisons
    return indices_out, scores_out, observed


def _chunked_select_rows(
    rows: np.ndarray,
    k: int,
    chunk_size: int,
) -> tuple[list[np.ndarray], list[np.ndarray], int, dict[str, Any]]:
    indices_out: list[np.ndarray] = []
    scores_out: list[np.ndarray] = []
    observed = 0
    total_chunks = 0
    total_boundary = 0
    for row in rows:
        n = int(row.size)
        effective_chunk = effective_chunk_size(n, k, chunk_size)
        selector = _HeapSelector(k)
        chunks = 0
        boundary = 0
        for start in range(0, n, effective_chunk):
            stop = min(start + effective_chunk, n)
            local = row[start:stop]
            order = np.argsort(-local, kind="stable")[:k]
            observed += int(local.size * ceil_log2(max(local.size, 2)))
            for relative in order.tolist():
                absolute = start + int(relative)
                selector.consider((float(row[absolute]), -absolute, absolute))
                boundary += 1
            chunks += 1
        selected = selector.sorted_desc()
        observed += selector.comparisons
        indices_out.append(np.asarray([item[2] for item in selected], dtype=np.int64))
        scores_out.append(np.asarray([item[0] for item in selected], dtype=row.dtype))
        total_chunks += chunks
        total_boundary += boundary
    metadata = {
        "chunk_size": int(effective_chunk_size(rows.shape[1], k, chunk_size)),
        "chunk_count_per_row": int((rows.shape[1] + effective_chunk_size(rows.shape[1], k, chunk_size) - 1) // effective_chunk_size(rows.shape[1], k, chunk_size)),
        "total_chunks": total_chunks,
        "boundary_overhead": int(total_boundary),
        "algorithm": "local stable sort TopK -> streaming global heap -> final stable order",
    }
    return indices_out, scores_out, observed, metadata


def _estimate(route: str, vocab_size: int, batch: int, k: int, chunk_size: int) -> int:
    if route == "full_sort":
        return batch * vocab_size * ceil_log2(max(vocab_size, 2))
    if route == "heap_select":
        sift = ceil_log2(max(k + 1, 2))
        return batch * (vocab_size + max(vocab_size - k, 0) * sift + k * ceil_log2(max(k, 2)))
    chunk = effective_chunk_size(vocab_size, k, chunk_size)
    chunks = (vocab_size + chunk - 1) // chunk
    stage_one = sum(
        min(chunk, vocab_size - index * chunk)
        * ceil_log2(max(min(chunk, vocab_size - index * chunk), 2))
        for index in range(chunks)
    )
    merge = chunks * k * ceil_log2(max(k + 1, 2)) + k * ceil_log2(max(k, 2))
    return batch * (stage_one + merge)


def topk(
    logits: Any,
    k_value: Any = None,
    *,
    k: Any = None,
    route: str = "full_sort",
    tie_policy: str = "index_asc",
    chunk_size: int = 4096,
    case_id: str | None = None,
) -> TopKResult:
    """Select exact TopK candidates from one row or a batch of rows.

    Candidate scores are ordered descending.  Equal scores are ordered by
    ascending vocabulary index, as declared by the normalized ``tie_policy``.
    """

    if k_value is None:
        k_value = k
    elif k is not None:
        try:
            if int(k_value) != int(k):
                fail(
                    "E_K_INVALID",
                    "k and k_value disagree",
                    field="k_value",
                    case_id=case_id,
                    details={"k": k, "k_value": k_value},
                )
        except (TypeError, ValueError, OverflowError):
            pass
    if k_value is None:
        fail("E_K_INVALID", "k_value is required", field="k_value", case_id=case_id)
    rows = _as_float_rows(logits, case_id=case_id)
    k = validate_k(k_value, int(rows.shape[1]), case_id=case_id)
    normalized_route = normalize_route(route, case_id=case_id)
    if normalized_route == "auto":
        fail(
            "E_K_MODE_INVALID",
            "topk needs a concrete route; resolve auto through decide_route",
            field="route",
            case_id=case_id,
        )
    normalized_tie = normalize_tie_policy(tie_policy, case_id=case_id)
    estimate = _estimate(
        normalized_route,
        int(rows.shape[1]),
        int(rows.shape[0]),
        k,
        chunk_size,
    )

    if normalized_route == "full_sort":
        indices, scores, observed = _full_sort_rows(rows, k)
        metadata = {"algorithm": "stable full argsort", "stable": True}
    elif normalized_route == "heap_select":
        indices, scores, observed = _heap_select_rows(rows, k)
        metadata = {
            "algorithm": "explicit min-heap of (score, -index)",
            "heap_capacity": k,
            "stable_tie_break": "ascending index",
        }
    elif normalized_route == "chunked_two_stage":
        indices, scores, observed, metadata = _chunked_select_rows(rows, k, chunk_size)
    else:
        raise AssertionError(f"unhandled route {normalized_route}")

    result = _result_from_rows(
        normalized_route,
        indices,
        scores,
        comparison_estimate=estimate,
        comparison_observed=observed,
        tie_policy=normalized_tie,
        metadata=metadata,
    )
    duplicates = first_duplicate(result.indices)
    if duplicates is not None:
        fail(
            "E_TOPK_INCONSISTENT",
            "TopK route produced duplicate candidate indices",
            field="candidates",
            case_id=case_id,
            details={"route": normalized_route, "first_deviation": duplicates},
        )
    return result


def topk_all_routes(
    logits: Any,
    k_value: Any = None,
    *,
    k: Any = None,
    tie_policy: str = "index_asc",
    chunk_size: int = 4096,
    case_id: str | None = None,
) -> dict[str, TopKResult]:
    return {
        route: topk(
            logits,
            k_value if k_value is not None else k,
            route=route,
            tie_policy=tie_policy,
            chunk_size=chunk_size,
            case_id=case_id,
        )
        for route in ROUTES
    }


def first_duplicate(indices: np.ndarray) -> dict[str, int] | None:
    for row_index, row in enumerate(indices):
        seen: set[int] = set()
        for position, value in enumerate(row.tolist()):
            if value in seen:
                return {"row": row_index, "position": position, "index": int(value)}
            seen.add(value)
    return None


def first_deviation(actual: np.ndarray, expected: np.ndarray) -> dict[str, Any] | None:
    actual_rows, actual_width = actual.shape
    expected_rows, expected_width = expected.shape
    rows = min(actual_rows, expected_rows)
    for row_index in range(rows):
        width = min(actual_width, expected_width)
        for position in range(width):
            if int(actual[row_index, position]) != int(expected[row_index, position]):
                return {
                    "row": row_index,
                    "position": position,
                    "actual": int(actual[row_index, position]),
                    "expected": int(expected[row_index, position]),
                }
        if actual_width != expected_width:
            return {
                "row": row_index,
                "position": min(actual_width, expected_width),
                "actual_length": actual_width,
                "expected_length": expected_width,
            }
    if actual_rows != expected_rows:
        return {"row": rows, "position": 0, "actual_rows": actual_rows, "expected_rows": expected_rows}
    return None


def verify_route_consistency(
    results: dict[str, TopKResult],
    reference: TopKResult,
    *,
    case_id: str | None,
) -> dict[str, Any]:
    """Verify every route against the float64 full-sort reference."""

    report: dict[str, Any] = {"ok": True, "checks": {}, "first_failure": None}
    for route in ROUTES:
        if route not in results:
            report["ok"] = False
            report["first_failure"] = {
                "route": route,
                "kind": "missing_route",
                "position": 0,
                "case_id": case_id,
            }
            break
        result = results[route]
        duplicate = first_duplicate(result.indices)
        deviation = first_deviation(result.indices, reference.indices)
        check = {
            "duplicate": duplicate,
            "first_deviation": deviation,
            "candidate_count_ok": bool(result.indices.shape == reference.indices.shape),
        }
        report["checks"][route] = check
        if duplicate is not None:
            report["ok"] = False
            report["first_failure"] = {
                "route": route,
                "kind": "duplicate",
                "position": duplicate["position"],
                "row": duplicate["row"],
                "case_id": case_id,
            }
            break
        if deviation is not None:
            report["ok"] = False
            report["first_failure"] = {
                "route": route,
                "kind": "candidate_mismatch",
                **deviation,
                "case_id": case_id,
            }
            break
    return report

