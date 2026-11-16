"""Naive and fused vectorized execution plans."""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field

import numpy as np

from .errors import BenchError, ErrorCode
from .kernels import ValueColumn, apply_binary, apply_ternary, apply_unary, literal_column
from .model import DataBlock, dtype_of, width_of
from .parser import Node
from .typecheck import TypeAnalyzer

NAIVE = "naive"
FUSED = "fused"


@dataclass(frozen=True)
class ColumnSummary:
    label: str
    type_name: str
    rows: int
    null_count: int
    digest: str

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "type": self.type_name,
            "rows": self.rows,
            "null_count": self.null_count,
            "digest": self.digest,
        }


@dataclass
class QueryResult:
    query_index: int
    canonical: str
    path: str
    batch_size: int
    batch_count: int
    result_type: str
    values: np.ndarray
    nullmap: np.ndarray
    intermediate_summaries: list[ColumnSummary]
    division_zero_count: int
    elapsed_seconds: float
    peak_bytes_observed: int
    fused_segments: list[int] = field(default_factory=list)

    def result_summary(self) -> ColumnSummary:
        return summarize_column("result", ValueColumn(self.result_type, self.values, self.nullmap))

    def to_report(self) -> dict:
        return {
            "query_index": self.query_index,
            "canonical": self.canonical,
            "path": self.path,
            "batch_size": self.batch_size,
            "batch_count": self.batch_count,
            "result": self.result_summary().to_dict(),
            "intermediates": [item.to_dict() for item in self.intermediate_summaries],
            "division_zero_count": self.division_zero_count,
            "elapsed_seconds": round(self.elapsed_seconds, 9),
            "peak_bytes_observed": self.peak_bytes_observed,
            "fused_segments": list(self.fused_segments),
        }


def summarize_column(label: str, column: ValueColumn) -> ColumnSummary:
    hasher = hashlib.sha256()
    hasher.update(column.type_name.encode("utf-8"))
    hasher.update(np.ascontiguousarray(column.values).tobytes())
    hasher.update(np.ascontiguousarray(column.nullmap.astype(np.uint8)).tobytes())
    return ColumnSummary(
        label=label,
        type_name=column.type_name,
        rows=column.row_count,
        null_count=int(column.nullmap.sum()),
        digest=hasher.hexdigest(),
    )


def _node_label(node: Node, query_index: int) -> str:
    return f"q{query_index}:{node.structural_digest()[:10]}"


class AllocationTracker:
    """Tracks actual NumPy materialization widths (values + 1-byte nullmap)."""

    def __init__(self) -> None:
        self.live_bytes = 0
        self.peak_bytes = 0

    @staticmethod
    def column_bytes(column: ValueColumn) -> int:
        return column.row_count * (width_of(column.type_name) + 1)

    def add(self, column: ValueColumn) -> None:
        self.live_bytes += self.column_bytes(column)
        self.peak_bytes = max(self.peak_bytes, self.live_bytes)

    def release(self, column: ValueColumn) -> None:
        self.live_bytes -= self.column_bytes(column)


def _eval_node(
    node: Node,
    block: DataBlock,
    start: int,
    end: int,
    cache: dict[int, ValueColumn],
) -> ValueColumn:
    key = id(node)
    cached = cache.get(key)
    if cached is not None:
        return cached
    row_count = end - start
    if node.kind == "literal":
        column = literal_column(node, row_count)
    elif node.kind == "column":
        source = block.column(node.name)  # type: ignore[arg-type]
        column = ValueColumn(
            source.type_name,
            source.values[start:end],
            source.nullmap[start:end],
        )
    elif node.kind == "unary":
        column = apply_unary(node.op, _eval_node(node.children[0], block, start, end, cache))
    elif node.kind == "binary":
        column = apply_binary(
            node.op,
            _eval_node(node.children[0], block, start, end, cache),
            _eval_node(node.children[1], block, start, end, cache),
        )
    elif node.kind == "ternary":
        column = apply_ternary(
            _eval_node(node.children[0], block, start, end, cache),
            _eval_node(node.children[1], block, start, end, cache),
            _eval_node(node.children[2], block, start, end, cache),
        )
    else:  # pragma: no cover - parser guards this case
        raise BenchError(ErrorCode.E_EXPR, "unknown node kind", {"kind": node.kind})
    cache[key] = column
    return column


def _iter_batches(row_count: int, batch_size: int) -> list[tuple[int, int]]:
    if not isinstance(batch_size, int) or isinstance(batch_size, bool) or batch_size <= 0:
        raise BenchError(ErrorCode.E_BATCH, "batch size must be a positive integer",
                         {"batch_size": batch_size})
    return [(start, min(start + batch_size, row_count)) for start in range(0, row_count, batch_size)]


def _operator_nodes(root: Node) -> list[Node]:
    return [node for node in root.walk_postorder() if node.kind not in ("column", "literal")]


def run_naive(
    root: Node,
    block: DataBlock,
    analyzer: TypeAnalyzer,
    query_index: int,
    batch_size: int,
) -> QueryResult:
    started = time.perf_counter()
    batches = _iter_batches(block.row_count, batch_size)
    result_type = analyzer.typed[id(root)].result_type
    values = np.empty(block.row_count, dtype=dtype_of(result_type))
    nullmap = np.empty(block.row_count, dtype=np.dtype("bool"))
    tracker = AllocationTracker()
    operator_nodes = _operator_nodes(root)
    digest_parts: dict[str, list[str]] = {}
    rows: dict[str, int] = {}
    null_counts: dict[str, int] = {}
    types: dict[str, str] = {}
    division_zero_total = 0
    for start, end in batches:
        cache: dict[int, ValueColumn] = {}
        produced: list[ValueColumn] = []
        for node in operator_nodes:
            column = _eval_node(node, block, start, end, cache)
            tracker.add(column)
            produced.append(column)
            division_zero_total += int(column.division_zero.sum())
            label = _node_label(node, query_index)
            summary = summarize_column(label, column)
            digest_parts.setdefault(label, []).append(summary.digest)
            rows[label] = rows.get(label, 0) + summary.rows
            null_counts[label] = null_counts.get(label, 0) + summary.null_count
            types[label] = summary.type_name
        root_column = cache[id(root)]
        values[start:end] = root_column.values
        nullmap[start:end] = root_column.nullmap
        for column in produced:
            tracker.release(column)
    summaries = _finalize_summaries(operator_nodes, query_index, digest_parts, rows, null_counts, types)
    return QueryResult(
        query_index=query_index,
        canonical=root.canonical_text(),
        path=NAIVE,
        batch_size=batch_size,
        batch_count=len(batches),
        result_type=result_type,
        values=values,
        nullmap=nullmap,
        intermediate_summaries=summaries,
        division_zero_count=division_zero_total,
        elapsed_seconds=time.perf_counter() - started,
        peak_bytes_observed=tracker.peak_bytes,
    )


def build_fusion_segments(root: Node, analyzer: TypeAnalyzer) -> list[list[Node]]:
    """Partition postorder operator nodes into deterministic fused segments.

    Boundary rules (each forces a segment break):
      1. a ternary node is never fused across parent/child operators;
      2. a null-propagation edge (a child operator whose output is nullable)
         is retained and starts a boundary, because downstream null semantics
         must be auditable at that materialized root;
      3. adjacent operator outputs with different static types break fusion;
      4. a shared child subtree starts a new segment so each retained root is
         owned once. Literal/leaf nullability is handled inside the kernel and
         does not by itself force a break.
    """
    segments: list[list[Node]] = []
    current: list[Node] = []
    parent_count: dict[int, int] = {}
    for node in _operator_nodes(root):
        for child in node.children:
            if child.kind not in ("column", "literal"):
                parent_count[id(child)] = parent_count.get(id(child), 0) + 1

    def has_nullable_operator_child(node: Node) -> bool:
        for child in node.children:
            if child.kind not in ("column", "literal") and analyzer.typed[id(child)].nullable:
                return True
        return False

    def flush() -> None:
        nonlocal current
        if current:
            segments.append(current)
            current = []

    previous: Node | None = None
    for node in _operator_nodes(root):
        typed = analyzer.typed[id(node)]
        shared_entry = parent_count.get(id(node), 0) > 1
        if current and previous is not None:
            previous_typed = analyzer.typed[id(previous)]
            if (
                node.kind == "ternary"
                or previous.kind == "ternary"
                or has_nullable_operator_child(node)
                or previous_typed.result_type != typed.result_type
                or shared_entry
            ):
                flush()
        current.append(node)
        previous = node
        if node.kind == "ternary":
            flush()
            previous = None
    flush()
    return segments


def run_fused(
    root: Node,
    block: DataBlock,
    analyzer: TypeAnalyzer,
    query_index: int,
    batch_size: int,
) -> QueryResult:
    started = time.perf_counter()
    batches = _iter_batches(block.row_count, batch_size)
    result_type = analyzer.typed[id(root)].result_type
    values = np.empty(block.row_count, dtype=dtype_of(result_type))
    nullmap = np.empty(block.row_count, dtype=np.dtype("bool"))
    tracker = AllocationTracker()
    segments = build_fusion_segments(root, analyzer)
    segment_roots = {id(segment[-1]) for segment in segments}
    retained_nodes = [segment[-1] for segment in segments]
    digest_parts: dict[str, list[str]] = {}
    rows: dict[str, int] = {}
    null_counts: dict[str, int] = {}
    types: dict[str, str] = {}
    division_zero_total = 0
    for start, end in batches:
        cache: dict[int, ValueColumn] = {}
        retained_columns: list[ValueColumn] = []
        for node in _operator_nodes(root):
            column = _eval_node(node, block, start, end, cache)
            division_zero_total += int(column.division_zero.sum())
            if id(node) in segment_roots:
                tracker.add(column)
                retained_columns.append(column)
                label = _node_label(node, query_index)
                summary = summarize_column(label, column)
                digest_parts.setdefault(label, []).append(summary.digest)
                rows[label] = rows.get(label, 0) + summary.rows
                null_counts[label] = null_counts.get(label, 0) + summary.null_count
                types[label] = summary.type_name
        root_column = cache[id(root)]
        values[start:end] = root_column.values
        nullmap[start:end] = root_column.nullmap
        for column in retained_columns:
            tracker.release(column)
    summaries = _finalize_summaries(retained_nodes, query_index, digest_parts, rows, null_counts, types)
    return QueryResult(
        query_index=query_index,
        canonical=root.canonical_text(),
        path=FUSED,
        batch_size=batch_size,
        batch_count=len(batches),
        result_type=result_type,
        values=values,
        nullmap=nullmap,
        intermediate_summaries=summaries,
        division_zero_count=division_zero_total,
        elapsed_seconds=time.perf_counter() - started,
        peak_bytes_observed=tracker.peak_bytes,
        fused_segments=[len(segment) for segment in segments],
    )


def _finalize_summaries(
    ordered_nodes: list[Node],
    query_index: int,
    digest_parts: dict[str, list[str]],
    rows: dict[str, int],
    null_counts: dict[str, int],
    types: dict[str, str],
) -> list[ColumnSummary]:
    summaries: list[ColumnSummary] = []
    for node in ordered_nodes:
        label = _node_label(node, query_index)
        digest = hashlib.sha256("".join(digest_parts[label]).encode("ascii")).hexdigest()
        summaries.append(ColumnSummary(label, types[label], rows[label], null_counts[label], digest))
    return summaries


def assert_byte_identical(expected: QueryResult, actual: QueryResult) -> None:
    if expected.result_type != actual.result_type:
        raise BenchError(
            ErrorCode.E_FUSE,
            "fused result type diverges",
            {"query_index": expected.query_index, "naive": expected.result_type,
             "fused": actual.result_type, "first_row": 0},
        )
    if not np.array_equal(expected.nullmap, actual.nullmap):
        first = int(np.flatnonzero(expected.nullmap != actual.nullmap)[0])
        raise BenchError(ErrorCode.E_FUSE, "fused nullmap diverges",
                         {"query_index": expected.query_index, "first_row": first})
    if not np.array_equal(expected.values, actual.values):
        first = int(np.flatnonzero(expected.values != actual.values)[0])
        raise BenchError(ErrorCode.E_FUSE, "fused values diverges",
                         {"query_index": expected.query_index, "first_row": first})
