from __future__ import annotations

import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from .cache import CacheCorruptError, CacheEntry, CacheStore
from .compute import (
    execute_node,
    merge_online_state,
    protect_norm,
    quantize_stats,
)
from .errors import BusinessError, ErrorCode, Issue, ResourceLimitError
from .graph import AttentionGraph, NodeSpec, build_attention_graph
from .hashing import array_content_hash, chunk_hash
from .models import (
    AttentionCase,
    QKVArrays,
    RuntimeConfig,
    SoftmaxConfig,
)
from .validation import validate_case, validate_cases


@dataclass
class NodeOutcome:
    node_id: str
    kind: str
    query_block: int | None
    kv_block: int | None
    cache_key: str
    content_hash: str
    checksum: str
    key_version: int
    arrays: dict[str, np.ndarray] = field(repr=False)
    metadata: dict[str, Any] = field(default_factory=dict)
    cache_hit: bool = False
    recomputed: bool = False
    forced: bool = False
    cache_written: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "kind": self.kind,
            "query_block": self.query_block,
            "kv_block": self.kv_block,
            "cache_key": self.cache_key,
            "content_hash": self.content_hash,
            "checksum": self.checksum,
            "key_version": self.key_version,
            "cache_hit": self.cache_hit,
            "recomputed": self.recomputed,
            "forced": self.forced,
            "cache_written": self.cache_written,
            "array_names": sorted(self.arrays),
        }


@dataclass
class AttentionResult:
    case_id: str
    ok: bool
    output: np.ndarray | None
    errors: list[Issue] = field(default_factory=list)
    warnings: list[Issue] = field(default_factory=list)
    unattributed_errors: list[Issue] = field(default_factory=list)
    node_outcomes: dict[str, NodeOutcome] = field(default_factory=dict)
    recomputed_nodes: tuple[str, ...] = ()
    reused_nodes: tuple[str, ...] = ()
    impacted_nodes: tuple[str, ...] = ()
    degraded: bool = False
    degradation_categories: tuple[str, ...] = ()
    resident_bytes_estimate: int = 0
    output_checksum: str | None = None
    graph: AttentionGraph | None = field(default=None, repr=False)
    arrays: QKVArrays | None = field(default=None, repr=False)

    @property
    def fatal_errors(self) -> list[Issue]:
        return [issue for issue in self.errors if not issue.recovered]

    @property
    def error_codes(self) -> tuple[str, ...]:
        return tuple(issue.code for issue in self.errors)

    def to_dict(self, *, include_nodes: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "case_id": self.case_id,
            "ok": self.ok,
            "errors": [issue.to_dict() for issue in self.errors],
            "warnings": [issue.to_dict() for issue in self.warnings],
            "unattributed_errors": [
                issue.to_dict() for issue in self.unattributed_errors
            ],
            "recomputed_nodes": list(self.recomputed_nodes),
            "reused_nodes": list(self.reused_nodes),
            "impacted_nodes": list(self.impacted_nodes),
            "degraded": self.degraded,
            "degradation_categories": list(self.degradation_categories),
            "resident_bytes_estimate": self.resident_bytes_estimate,
            "output_checksum": self.output_checksum,
            "graph": self.graph.summary() if self.graph is not None else None,
        }
        if self.output is not None:
            payload["output_shape"] = list(self.output.shape)
            payload["output_dtype"] = self.output.dtype.name
        if include_nodes:
            payload["nodes"] = {
                node_id: outcome.to_dict()
                for node_id, outcome in sorted(self.node_outcomes.items())
            }
        return payload


@dataclass
class IncrementalResult:
    baseline_case_id: str
    updated_case_id: str
    attention: AttentionResult
    affected_nodes: tuple[str, ...] = ()
    recomputed_nodes: tuple[str, ...] = ()
    reused_nodes: tuple[str, ...] = ()
    invalidation_roots: tuple[str, ...] = ()
    changed_kv_blocks: tuple[int, ...] = ()
    changed_q_blocks: tuple[int, ...] = ()
    structural_invalidation: bool = False
    self_check: "SelfCheckReport | None" = None

    @property
    def ok(self) -> bool:
        return self.attention.ok and (
            self.self_check is None or self.self_check.ok
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "baseline_case_id": self.baseline_case_id,
            "updated_case_id": self.updated_case_id,
            "ok": self.ok,
            "attention": self.attention.to_dict(),
            "affected_nodes": list(self.affected_nodes),
            "recomputed_nodes": list(self.recomputed_nodes),
            "reused_nodes": list(self.reused_nodes),
            "invalidation_roots": list(self.invalidation_roots),
            "changed_kv_blocks": list(self.changed_kv_blocks),
            "changed_q_blocks": list(self.changed_q_blocks),
            "structural_invalidation": self.structural_invalidation,
            "self_check": (
                self.self_check.to_dict()
                if self.self_check is not None
                else None
            ),
        }


@dataclass
class SelfCheckReport:
    case_id: str
    ok: bool
    issues: list[Issue] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "ok": self.ok,
            "issues": [issue.to_dict() for issue in self.issues],
        }


@dataclass
class BatchResult:
    ok: bool
    results: list[AttentionResult]
    errors: list[Issue] = field(default_factory=list)

    def to_dict(self, *, include_nodes: bool = False) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "errors": [issue.to_dict() for issue in self.errors],
            "results": [
                result.to_dict(include_nodes=include_nodes)
                for result in self.results
            ],
        }


@dataclass
class _NodeRun:
    outcome: NodeOutcome | None = None
    issues: list[Issue] = field(default_factory=list)
    forced_descendants: tuple[str, ...] = ()
    failed: bool = False


class FlashAttentionEngine:
    def __init__(
        self,
        cache_dir: str | Path,
        *,
        softmax: SoftmaxConfig | None = None,
        runtime: RuntimeConfig | None = None,
    ):
        self.softmax = softmax or SoftmaxConfig()
        self.runtime = runtime or RuntimeConfig()
        self.cache = CacheStore(
            cache_dir,
            max_entries=self.runtime.max_cache_entries,
            lock_timeout_s=self.runtime.cache_lock_timeout_s,
            lock_stale_s=self.runtime.cache_lock_stale_s,
        )

    def _config_issues(self, case_id: str | None) -> list[Issue]:
        issues = list(self.softmax.validate(case_id))
        issues.extend(self.runtime.validate(case_id))
        return issues

    def _build_graph(
        self,
        case: AttentionCase,
        arrays: QKVArrays,
    ) -> tuple[AttentionGraph | None, list[Issue]]:
        issues = self._config_issues(case.case_id)
        issues.extend(validate_case(case, arrays))
        if issues:
            return None, issues
        try:
            graph = build_attention_graph(case, arrays, self.softmax)
        except Exception as exc:  # pragma: no cover - defensive conversion
            return None, [
                Issue(
                    ErrorCode.NODE_FAILURE,
                    f"failed to build attention graph: {exc}",
                    case_id=case.case_id,
                    category="graph_build",
                    details={
                        "exception_type": type(exc).__name__,
                        "traceback": traceback.format_exc(limit=8),
                    },
                )
            ]
        return graph, []

    def compute(
        self,
        case: AttentionCase,
        arrays: QKVArrays,
        *,
        force_recompute: bool = False,
    ) -> AttentionResult:
        try:
            return self._compute_impl(
                case,
                arrays,
                force_recompute=force_recompute,
            )
        except BusinessError as exc:
            issue = exc.issue
            issue.case_id = issue.case_id or case.case_id
            unattributed = [] if issue.node_id else [issue]
            return AttentionResult(
                case_id=case.case_id,
                ok=False,
                output=None,
                errors=[issue],
                unattributed_errors=unattributed,
            )
        except Exception as exc:
            return self._unexpected_result(case.case_id, exc, "compute")

    def _unexpected_result(
        self,
        case_id: str,
        exc: Exception,
        operation: str,
    ) -> AttentionResult:
        issue = Issue(
            ErrorCode.NODE_FAILURE,
            f"unexpected {operation} failure: {exc}",
            case_id=case_id,
            category="unattributed_exception",
            details={
                "exception_type": type(exc).__name__,
                "traceback": traceback.format_exc(limit=12),
            },
        )
        return AttentionResult(
            case_id=case_id,
            ok=False,
            output=None,
            errors=[issue],
            unattributed_errors=[issue],
        )

    def _compute_impl(
        self,
        case: AttentionCase,
        arrays: QKVArrays,
        *,
        force_recompute: bool = False,
    ) -> AttentionResult:
        graph, issues = self._build_graph(case, arrays)
        if graph is None:
            return AttentionResult(
                case_id=case.case_id,
                ok=False,
                output=None,
                errors=issues,
            )
        resident_estimate = graph.estimate_resident_bytes(arrays)
        if resident_estimate > self.runtime.max_resident_bytes:
            issue = Issue(
                ErrorCode.RESOURCE_LIMIT,
                "estimated resident block memory exceeds configured limit",
                case_id=case.case_id,
                category="resident_memory",
                details={
                    "estimate_bytes": resident_estimate,
                    "max_resident_bytes": self.runtime.max_resident_bytes,
                },
            )
            return AttentionResult(
                case_id=case.case_id,
                ok=False,
                output=None,
                errors=[issue],
                resident_bytes_estimate=resident_estimate,
                graph=graph,
                arrays=arrays,
            )
        degradation: list[str] = []
        if self.runtime.parallel_limited:
            degradation.append("parallelism_limited")
        return self._execute_graph(
            case,
            arrays,
            graph,
            force_recompute=force_recompute,
            resident_estimate=resident_estimate,
            degradation=degradation,
            impacted=None,
        )

    def _execute_graph(
        self,
        case: AttentionCase,
        arrays: QKVArrays,
        graph: AttentionGraph,
        *,
        force_recompute: bool,
        resident_estimate: int,
        degradation: list[str],
        impacted: set[str] | None,
    ) -> AttentionResult:
        workers = min(
            self.runtime.effective_parallel_nodes,
            max(1, len(graph.nodes)),
        )
        outcomes: dict[str, NodeOutcome] = {}
        errors: list[Issue] = []
        warnings: list[Issue] = []
        unattributed: list[Issue] = []
        failed: set[str] = set()
        forced: set[str] = set(graph.nodes) if force_recompute else set()
        pending: set[str] = set(graph.order)
        fatal = False

        with ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix=f"fac-{case.case_id}",
        ) as pool:
            while pending:
                ready = sorted(
                    node_id
                    for node_id in pending
                    if all(
                        upstream_id in outcomes or upstream_id in failed
                        for upstream_id in graph.nodes[node_id].upstream
                    )
                )
                if not ready:
                    if pending:
                        issue = Issue(
                            ErrorCode.INCONSISTENT,
                            "graph scheduler reached a deadlock",
                            case_id=case.case_id,
                            category="scheduler_deadlock",
                        )
                        errors.append(issue)
                        fatal = True
                    break
                runnable: list[str] = []
                for node_id in ready:
                    node = graph.nodes[node_id]
                    failed_upstream = [
                        upstream_id
                        for upstream_id in node.upstream
                        if upstream_id in failed
                    ]
                    if failed_upstream:
                        issue = Issue(
                            ErrorCode.UPSTREAM_FAILED,
                            "node skipped because upstream failed",
                            case_id=case.case_id,
                            node_id=node_id,
                            block_index=node.block_index,
                            category="upstream_failed",
                            details={"upstream": failed_upstream},
                        )
                        errors.append(issue)
                        failed.add(node_id)
                        pending.remove(node_id)
                    else:
                        runnable.append(node_id)
                if not runnable:
                    continue
                future_map = {
                    pool.submit(
                        self._run_node,
                        case,
                        graph,
                        graph.nodes[node_id],
                        outcomes,
                        node_id in forced,
                    ): node_id
                    for node_id in runnable
                }
                for future, node_id in sorted(
                    future_map.items(),
                    key=lambda item: item[1],
                ):
                    pending.remove(node_id)
                    try:
                        run = future.result()
                    except Exception as exc:  # pragma: no cover - defensive
                        issue = Issue(
                            ErrorCode.NODE_FAILURE,
                            f"unhandled node worker failure: {exc}",
                            case_id=case.case_id,
                            node_id=node_id,
                            block_index=graph.nodes[node_id].block_index,
                            category="worker_failure",
                            details={
                                "exception_type": type(exc).__name__,
                                "traceback": traceback.format_exc(limit=8),
                            },
                        )
                        errors.append(issue)
                        failed.add(node_id)
                        fatal = True
                        continue
                    errors.extend(run.issues)
                    forced.update(run.forced_descendants)
                    if run.outcome is None or run.failed:
                        failed.add(node_id)
                        if any(not issue.recovered for issue in run.issues):
                            fatal = True
                    else:
                        outcomes[node_id] = run.outcome
                    if any(
                        issue.code == ErrorCode.RESOURCE_LIMIT and not issue.recovered
                        for issue in run.issues
                    ):
                        fatal = True

        output: np.ndarray | None = None
        output_checksum: str | None = None
        if not fatal and len(outcomes) == len(graph.nodes):
            finalize_nodes = sorted(
                (
                    node_id
                    for node_id, node in graph.nodes.items()
                    if node.kind == "finalize"
                ),
                key=lambda node_id: graph.nodes[node_id].query_block or 0,
            )
            blocks: list[np.ndarray] = []
            try:
                for node_id in finalize_nodes:
                    blocks.append(outcomes[node_id].arrays["out"])
                output = np.ascontiguousarray(
                    np.concatenate(blocks, axis=1)
                )
                if output.shape != (
                    case.n_heads,
                    case.seq_len,
                    case.head_dim,
                ):
                    raise ValueError(
                        f"assembled output shape {output.shape} is invalid"
                    )
                if not np.isfinite(output).all():
                    raise BusinessError(
                        Issue(
                            ErrorCode.SOFTMAX_NAN,
                            "assembled attention output contains non-finite values",
                            case_id=case.case_id,
                            category="nonfinite_output",
                        )
                    )
                output_checksum = array_content_hash(
                    {"out": output},
                    {"case_id": case.case_id},
                )
            except BusinessError as exc:
                errors.append(exc.issue)
                output = None
                fatal = True
            except Exception as exc:
                errors.append(
                    Issue(
                        ErrorCode.OUTPUT_MISSING,
                        f"could not assemble final output: {exc}",
                        case_id=case.case_id,
                        category="output_assembly",
                    )
                )
                output = None
                fatal = True
        elif not fatal:
            fatal = True
            errors.append(
                Issue(
                    ErrorCode.OUTPUT_MISSING,
                    "not every graph node produced an artifact",
                    case_id=case.case_id,
                    category="missing_nodes",
                    details={
                        "missing": sorted(set(graph.nodes) - set(outcomes))
                    },
                )
            )

        recomputed = tuple(
            node_id
            for node_id in graph.order
            if node_id in outcomes and outcomes[node_id].recomputed
        )
        reused = tuple(
            node_id
            for node_id in graph.order
            if node_id in outcomes and outcomes[node_id].cache_hit
        )
        unattributed = [
            issue for issue in errors if issue.node_id is None
        ]
        result = AttentionResult(
            case_id=case.case_id,
            ok=not fatal and output is not None,
            output=output,
            errors=errors,
            warnings=warnings,
            unattributed_errors=unattributed,
            node_outcomes=outcomes,
            recomputed_nodes=recomputed,
            reused_nodes=reused,
            impacted_nodes=tuple(sorted(impacted or ())),
            degraded=bool(degradation),
            degradation_categories=tuple(sorted(set(degradation))),
            resident_bytes_estimate=resident_estimate,
            output_checksum=output_checksum,
            graph=graph,
            arrays=arrays,
        )
        return result

    def _run_node(
        self,
        case: AttentionCase,
        graph: AttentionGraph,
        node: NodeSpec,
        outcomes: Mapping[str, NodeOutcome],
        force: bool,
    ) -> _NodeRun:
        upstream_identities = {
            upstream_id: {
                "content_hash": outcomes[upstream_id].content_hash,
                "cache_key": outcomes[upstream_id].cache_key,
            }
            for upstream_id in node.upstream
        }
        try:
            cache_key = node.cache_key(upstream_identities)
        except Exception as exc:  # pragma: no cover - defensive
            return _NodeRun(
                issues=[
                    Issue(
                        ErrorCode.INCONSISTENT,
                        f"cache key construction failed: {exc}",
                        case_id=case.case_id,
                        node_id=node.node_id,
                        block_index=node.block_index,
                        category="cache_key",
                    )
                ],
                failed=True,
            )

        corrupt_issues: list[Issue] = []
        force_descendants: set[str] = set()
        if not force:
            try:
                entry = self.cache.get(cache_key)
            except CacheCorruptError as exc:
                issue = exc.issue
                issue.case_id = case.case_id
                issue.node_id = node.node_id
                issue.block_index = node.block_index
                issue.recovered = True
                corrupt_issues.append(issue)
                force = True
            except ResourceLimitError as exc:
                issue = exc.issue
                issue.case_id = case.case_id
                issue.node_id = node.node_id
                issue.block_index = node.block_index
                return _NodeRun(issues=[issue], failed=True)
            except Exception as exc:
                return _NodeRun(
                    issues=[
                        Issue(
                            ErrorCode.CACHE_CORRUPT,
                            f"cache lookup failed unexpectedly: {exc}",
                            case_id=case.case_id,
                            node_id=node.node_id,
                            block_index=node.block_index,
                            category="cache_io",
                            details={
                                "exception_type": type(exc).__name__,
                            },
                        )
                    ],
                    failed=True,
                )
            else:
                if entry is not None:
                    outcome = NodeOutcome(
                        node_id=node.node_id,
                        kind=node.kind,
                        query_block=node.query_block,
                        kv_block=node.kv_block,
                        cache_key=cache_key,
                        content_hash=entry.content_hash,
                        checksum=entry.checksum,
                        key_version=entry.key_version,
                        arrays=entry.arrays,
                        metadata=entry.metadata,
                        cache_hit=True,
                        recomputed=False,
                        forced=False,
                        cache_written=False,
                    )
                    return _NodeRun(
                        outcome=outcome,
                        issues=corrupt_issues,
                    )

        if corrupt_issues:
            force_descendants = set(graph.descendants([node.node_id]))
            force_descendants.discard(node.node_id)
        try:
            upstream_arrays = {
                upstream_id: outcomes[upstream_id].arrays
                for upstream_id in node.upstream
            }
            arrays, metadata = execute_node(
                node,
                upstream_arrays,
                self.softmax,
                case.case_id,
            )
            canonical_arrays = {
                name: np.ascontiguousarray(
                    np.asarray(array, dtype=np.float64)
                )
                for name, array in arrays.items()
            }
            metadata = dict(metadata)
            content_hash = array_content_hash(canonical_arrays, metadata)
            try:
                entry = self.cache.put(
                    cache_key,
                    canonical_arrays,
                    metadata,
                    source_node_id=node.node_id,
                    content_hash=content_hash,
                )
            except ResourceLimitError as exc:
                issue = exc.issue
                issue.case_id = case.case_id
                issue.node_id = node.node_id
                issue.block_index = node.block_index
                return _NodeRun(issues=[*corrupt_issues, issue], failed=True)
            outcome = NodeOutcome(
                node_id=node.node_id,
                kind=node.kind,
                query_block=node.query_block,
                kv_block=node.kv_block,
                cache_key=cache_key,
                content_hash=content_hash,
                checksum=entry.checksum,
                key_version=entry.key_version,
                arrays=canonical_arrays,
                metadata=entry.metadata,
                cache_hit=False,
                recomputed=True,
                forced=force,
                cache_written=entry.wrote,
            )
            return _NodeRun(
                outcome=outcome,
                issues=corrupt_issues,
                forced_descendants=tuple(sorted(force_descendants)),
            )
        except BusinessError as exc:
            issue = exc.issue
            issue.case_id = issue.case_id or case.case_id
            issue.node_id = issue.node_id or node.node_id
            if issue.block_index is None:
                issue.block_index = node.block_index
            return _NodeRun(
                issues=[*corrupt_issues, issue],
                forced_descendants=tuple(sorted(force_descendants)),
                failed=True,
            )
        except Exception as exc:
            return _NodeRun(
                issues=[
                    *corrupt_issues,
                    Issue(
                        ErrorCode.NODE_FAILURE,
                        f"node execution failed unexpectedly: {exc}",
                        case_id=case.case_id,
                        node_id=node.node_id,
                        block_index=node.block_index,
                        category="unexpected_exception",
                        details={
                            "exception_type": type(exc).__name__,
                            "traceback": traceback.format_exc(limit=8),
                        },
                    ),
                ],
                forced_descendants=tuple(sorted(force_descendants)),
                failed=True,
            )

    def incremental_update(
        self,
        previous_case: AttentionCase,
        previous_arrays: QKVArrays,
        updated_case: AttentionCase,
        updated_arrays: QKVArrays,
        *,
        baseline_result: AttentionResult | None = None,
        verify_self_check: bool | None = None,
    ) -> IncrementalResult:
        try:
            return self._incremental_update_impl(
                previous_case,
                previous_arrays,
                updated_case,
                updated_arrays,
                baseline_result=baseline_result,
                verify_self_check=verify_self_check,
            )
        except BusinessError as exc:
            issue = exc.issue
            issue.case_id = issue.case_id or updated_case.case_id
            return IncrementalResult(
                baseline_case_id=previous_case.case_id,
                updated_case_id=updated_case.case_id,
                attention=AttentionResult(
                    case_id=updated_case.case_id,
                    ok=False,
                    output=None,
                    errors=[issue],
                    unattributed_errors=[] if issue.node_id else [issue],
                ),
            )
        except Exception as exc:
            return IncrementalResult(
                baseline_case_id=previous_case.case_id,
                updated_case_id=updated_case.case_id,
                attention=self._unexpected_result(
                    updated_case.case_id,
                    exc,
                    "incremental_update",
                ),
            )

    def _incremental_update_impl(
        self,
        previous_case: AttentionCase,
        previous_arrays: QKVArrays,
        updated_case: AttentionCase,
        updated_arrays: QKVArrays,
        *,
        baseline_result: AttentionResult | None = None,
        verify_self_check: bool | None = None,
    ) -> IncrementalResult:
        if baseline_result is None:
            baseline_result = self.compute(previous_case, previous_arrays)
        if not baseline_result.ok:
            issue = Issue(
                ErrorCode.INCONSISTENT,
                "baseline computation failed; incremental update cannot proceed",
                case_id=previous_case.case_id,
                category="baseline_failed",
                details={
                    "baseline_errors": [
                        item.to_dict() for item in baseline_result.errors
                    ]
                },
            )
            return IncrementalResult(
                baseline_case_id=previous_case.case_id,
                updated_case_id=updated_case.case_id,
                attention=AttentionResult(
                    case_id=updated_case.case_id,
                    ok=False,
                    output=None,
                    errors=[issue],
                ),
            )
        graph, issues = self._build_graph(updated_case, updated_arrays)
        if graph is None:
            return IncrementalResult(
                baseline_case_id=previous_case.case_id,
                updated_case_id=updated_case.case_id,
                attention=AttentionResult(
                    case_id=updated_case.case_id,
                    ok=False,
                    output=None,
                    errors=issues,
                ),
            )

        structural, changed_kv, changed_q = self._changed_blocks(
            previous_case,
            previous_arrays,
            updated_case,
            updated_arrays,
        )
        roots: set[str] = set()
        if structural:
            impacted = set(graph.nodes)
            roots = set(graph.nodes)
        else:
            width = max(4, len(str(max(0, updated_case.num_kv_blocks - 1))))
            for kv_block in changed_kv:
                for kind in ("split_kv", "kvload"):
                    root = f"{kind}:{kv_block:0{width}d}"
                    if root in graph.nodes:
                        roots.add(root)
            for query_block in changed_q:
                for kind in ("split_q", "qproj"):
                    root = f"{kind}:{query_block:0{width}d}"
                    if root in graph.nodes:
                        roots.add(root)
            impacted = graph.descendants(roots)

        resident_estimate = graph.estimate_resident_bytes(updated_arrays)
        if resident_estimate > self.runtime.max_resident_bytes:
            issue = Issue(
                ErrorCode.RESOURCE_LIMIT,
                "estimated resident block memory exceeds configured limit",
                case_id=updated_case.case_id,
                category="resident_memory",
                details={
                    "estimate_bytes": resident_estimate,
                    "max_resident_bytes": self.runtime.max_resident_bytes,
                },
            )
            attention = AttentionResult(
                case_id=updated_case.case_id,
                ok=False,
                output=None,
                errors=[issue],
                resident_bytes_estimate=resident_estimate,
                graph=graph,
                arrays=updated_arrays,
            )
            return IncrementalResult(
                baseline_case_id=previous_case.case_id,
                updated_case_id=updated_case.case_id,
                attention=attention,
                affected_nodes=tuple(sorted(impacted)),
                invalidation_roots=tuple(sorted(roots)),
                changed_kv_blocks=tuple(changed_kv),
                changed_q_blocks=tuple(changed_q),
                structural_invalidation=structural,
            )

        degradation: list[str] = []
        if self.runtime.parallel_limited:
            degradation.append("parallelism_limited")
        attention = self._execute_graph(
            updated_case,
            updated_arrays,
            graph,
            force_recompute=False,
            resident_estimate=resident_estimate,
            degradation=degradation,
            impacted=impacted,
        )

        report = IncrementalResult(
            baseline_case_id=previous_case.case_id,
            updated_case_id=updated_case.case_id,
            attention=attention,
            affected_nodes=tuple(sorted(impacted)),
            recomputed_nodes=attention.recomputed_nodes,
            reused_nodes=attention.reused_nodes,
            invalidation_roots=tuple(sorted(roots)),
            changed_kv_blocks=tuple(changed_kv),
            changed_q_blocks=tuple(changed_q),
            structural_invalidation=structural,
        )
        should_check = (
            self.runtime.self_check_enabled
            if verify_self_check is None
            else verify_self_check
        )
        if should_check and attention.ok:
            report.self_check = self.self_check(
                updated_case,
                updated_arrays,
                attention,
                incremental=report,
            )
        return report

    def _changed_blocks(
        self,
        previous_case: AttentionCase,
        previous_arrays: QKVArrays,
        updated_case: AttentionCase,
        updated_arrays: QKVArrays,
    ) -> tuple[bool, list[int], list[int]]:
        structural_fields = (
            previous_case.head_dim != updated_case.head_dim,
            previous_case.n_heads != updated_case.n_heads,
            previous_case.block_size != updated_case.block_size,
            previous_case.causal != updated_case.causal,
            previous_case.effective_mask_mode
            != updated_case.effective_mask_mode,
            previous_case.dtype != updated_case.dtype,
            previous_case.seed != updated_case.seed,
            previous_case.scale != updated_case.scale,
        )
        structural = any(structural_fields)
        if structural:
            return True, [], []
        block_size = updated_case.block_size
        old_blocks = previous_case.num_kv_blocks
        new_blocks = updated_case.num_kv_blocks
        if old_blocks != new_blocks and updated_case.seq_len < previous_case.seq_len:
            return True, [], []
        changed_kv: list[int] = []
        old_k64 = np.asarray(previous_arrays.k, dtype=np.float64)
        old_v64 = np.asarray(previous_arrays.v, dtype=np.float64)
        new_k64 = np.asarray(updated_arrays.k, dtype=np.float64)
        new_v64 = np.asarray(updated_arrays.v, dtype=np.float64)
        for block in range(new_blocks):
            if block >= old_blocks:
                changed_kv.append(block)
                continue
            start = block * block_size
            old_len = min(
                block_size,
                previous_case.seq_len - start,
            )
            new_len = min(
                block_size,
                updated_case.seq_len - start,
            )
            if old_len != new_len:
                changed_kv.append(block)
                continue
            old_revision = previous_case.revision_for(block)
            new_revision = updated_case.revision_for(block)
            old_k_hash = chunk_hash(
                old_k64[:, start : start + old_len, :],
                start=start,
                length=old_len,
                role="k",
            )
            new_k_hash = chunk_hash(
                new_k64[:, start : start + new_len, :],
                start=start,
                length=new_len,
                role="k",
            )
            old_v_hash = chunk_hash(
                old_v64[:, start : start + old_len, :],
                start=start,
                length=old_len,
                role="v",
            )
            new_v_hash = chunk_hash(
                new_v64[:, start : start + new_len, :],
                start=start,
                length=new_len,
                role="v",
            )
            if (
                old_revision != new_revision
                or old_k_hash != new_k_hash
                or old_v_hash != new_v_hash
            ):
                changed_kv.append(block)

        changed_q: list[int] = []
        old_q64 = np.asarray(previous_arrays.q, dtype=np.float64)
        new_q64 = np.asarray(updated_arrays.q, dtype=np.float64)
        for block in range(new_blocks):
            if block >= previous_case.num_kv_blocks:
                changed_q.append(block)
                continue
            start = block * block_size
            old_len = min(block_size, previous_case.seq_len - start)
            new_len = min(block_size, updated_case.seq_len - start)
            if old_len != new_len:
                changed_q.append(block)
                continue
            old_hash = chunk_hash(
                old_q64[:, start : start + old_len, :],
                start=start,
                length=old_len,
                role="q",
            )
            new_hash = chunk_hash(
                new_q64[:, start : start + new_len, :],
                start=start,
                length=new_len,
                role="q",
            )
            if old_hash != new_hash:
                changed_q.append(block)
        return False, changed_kv, changed_q

    def self_check(
        self,
        case: AttentionCase,
        arrays: QKVArrays,
        result: AttentionResult,
        *,
        incremental: IncrementalResult | None = None,
    ) -> SelfCheckReport:
        issues: list[Issue] = []

        def add(
            category: str,
            message: str,
            *,
            node_id: str | None = None,
            block_index: int | tuple[int, ...] | None = None,
            details: Mapping[str, Any] | None = None,
        ) -> None:
            issues.append(
                Issue(
                    ErrorCode.INCONSISTENT,
                    message,
                    case_id=case.case_id,
                    node_id=node_id,
                    block_index=block_index,
                    category=category,
                    details=dict(details or {}),
                )
            )

        if not result.ok:
            add("result_failed", "self-check requires a successful result")
            return SelfCheckReport(case.case_id, False, issues)
        graph = result.graph
        if graph is None:
            add("graph_missing", "successful result has no graph")
            return SelfCheckReport(case.case_id, False, issues)
        graph_issues = graph.validate()
        for issue in graph_issues:
            issues.append(
                Issue(
                    ErrorCode.INCONSISTENT,
                    issue.message,
                    case_id=case.case_id,
                    node_id=issue.node_id,
                    block_index=issue.block_index,
                    category=issue.category or "graph_topology",
                    details={"underlying_code": issue.code},
                )
            )

        for node_id in graph.order:
            node = graph.nodes[node_id]
            outcome = result.node_outcomes.get(node_id)
            if outcome is None:
                add(
                    "missing_node_artifact",
                    "node artifact is missing from the result",
                    node_id=node_id,
                    block_index=node.block_index,
                )
                continue
            actual_hash = array_content_hash(outcome.arrays, outcome.metadata)
            if actual_hash != outcome.content_hash:
                add(
                    "content_hash_mismatch",
                    "node artifact hash does not match its arrays and metadata",
                    node_id=node_id,
                    block_index=node.block_index,
                )
            upstream_identities = {
                upstream_id: {
                    "content_hash": result.node_outcomes[
                        upstream_id
                    ].content_hash,
                    "cache_key": result.node_outcomes[upstream_id].cache_key,
                }
                for upstream_id in node.upstream
                if upstream_id in result.node_outcomes
            }
            try:
                expected_key = node.cache_key(upstream_identities)
            except Exception as exc:
                add(
                    "cache_key_mismatch",
                    f"could not recompute node cache key: {exc}",
                    node_id=node_id,
                    block_index=node.block_index,
                )
            else:
                if expected_key != outcome.cache_key:
                    add(
                        "cache_key_mismatch",
                        "recomputed cache key differs from node outcome",
                        node_id=node_id,
                        block_index=node.block_index,
                    )
            try:
                entry = self.cache.get(outcome.cache_key)
            except CacheCorruptError as exc:
                issue = exc.issue
                issue.case_id = case.case_id
                issue.node_id = node_id
                issue.block_index = node.block_index
                issues.append(issue)
                continue
            if entry is None:
                add(
                    "cache_entry_missing",
                    "cache entry for node result is missing",
                    node_id=node_id,
                    block_index=node.block_index,
                )
                continue
            if (
                entry.content_hash != outcome.content_hash
                or entry.key_version != outcome.key_version
                or entry.checksum != outcome.checksum
            ):
                add(
                    "cache_entry_mismatch",
                    "cache entry metadata does not match node outcome",
                    node_id=node_id,
                    block_index=node.block_index,
                    details={
                        "entry_content_hash": entry.content_hash,
                        "outcome_content_hash": outcome.content_hash,
                        "entry_key_version": entry.key_version,
                        "outcome_key_version": outcome.key_version,
                    },
                )

        recomputed: set[str] = set()
        reused: set[str] = set()
        if incremental is not None:
            recomputed = set(incremental.recomputed_nodes)
            reused = set(incremental.reused_nodes)
            overlap = recomputed & reused
            if overlap:
                add(
                    "node_set_overlap",
                    "recomputed and reused node sets overlap",
                    details={"overlap": sorted(overlap)},
                )
            affected = set(incremental.affected_nodes)
            if not affected.issubset(recomputed | reused):
                add(
                    "affected_not_covered",
                    "affected nodes are not covered by recomputed or reused sets",
                    details={
                        "uncovered": sorted(affected - (recomputed | reused))
                    },
                )

        try:
            recomputed_output = self._recompute_from_block_details(
                case,
                arrays,
                graph,
                result,
            )
        except BusinessError as exc:
            issue = exc.issue
            issue.category = issue.category or "block_recompute"
            issue.case_id = issue.case_id or case.case_id
            issues.append(issue)
        except Exception as exc:
            add(
                "block_recompute",
                f"could not recompute output from block details: {exc}",
                details={"exception_type": type(exc).__name__},
            )
        else:
            if result.output is None:
                add("output_missing", "result output is missing")
            elif not np.allclose(
                recomputed_output,
                result.output,
                rtol=self.softmax.rtol,
                atol=self.softmax.atol,
                equal_nan=False,
            ):
                delta = np.abs(recomputed_output - result.output)
                add(
                    "output_mismatch",
                    "final output cannot be reconstructed from block details",
                    details={
                        "max_abs_delta": float(np.max(delta)),
                        "atol": self.softmax.atol,
                        "rtol": self.softmax.rtol,
                    },
                )
        return SelfCheckReport(case.case_id, not issues, issues)

    def _recompute_from_block_details(
        self,
        case: AttentionCase,
        arrays: QKVArrays,
        graph: AttentionGraph,
        result: AttentionResult,
    ) -> np.ndarray:
        v64 = np.asarray(arrays.v, dtype=np.float64)
        width = max(4, len(str(max(0, case.num_kv_blocks - 1))))
        blocks: list[np.ndarray] = []
        for query_block in range(case.num_kv_blocks):
            q_start = query_block * case.block_size
            q_len = min(case.block_size, case.seq_len - q_start)
            last_kv = query_block if case.causal else case.num_kv_blocks - 1
            previous_state: (
                tuple[np.ndarray, np.ndarray, np.ndarray] | None
            ) = None
            merged_sum: np.ndarray | None = None
            score_blocks: list[np.ndarray] = []
            for kv_block in range(last_kv + 1):
                score_id = f"score:{query_block:0{width}d}:{kv_block:0{width}d}"
                outcome = result.node_outcomes.get(score_id)
                if outcome is None:
                    raise BusinessError(
                        Issue(
                            ErrorCode.INCONSISTENT,
                            f"missing score artifact {score_id}",
                            case_id=case.case_id,
                            node_id=score_id,
                            block_index=(query_block, kv_block),
                            category="missing_score_block",
                        )
                    )
                scores = np.asarray(outcome.arrays["scores"], dtype=np.float64)
                score_blocks.append(scores)
                kv_start = kv_block * case.block_size
                kv_len = min(
                    case.block_size,
                    case.seq_len - kv_start,
                )
                v_chunk = np.ascontiguousarray(
                    v64[:, kv_start : kv_start + kv_len, :]
                )
                with np.errstate(over="ignore", invalid="ignore", under="ignore"):
                    row_max = quantize_stats(
                        np.max(scores, axis=-1),
                        self.softmax,
                    )
                    weights = np.exp(scores - row_max[..., None])
                    row_sum = protect_norm(
                        quantize_stats(
                            np.sum(weights, axis=-1),
                            self.softmax,
                        ),
                        self.softmax,
                        node_id=f"selfcheck:softmax:{query_block}:{kv_block}",
                        block_index=(query_block, kv_block),
                        case_id=case.case_id,
                    )
                    chunk_acc = np.matmul(weights, v_chunk)
                previous_state = merge_online_state(
                    previous_state,
                    row_max,
                    row_sum,
                    chunk_acc,
                    self.softmax,
                    node_id=f"selfcheck:merge:{query_block}:{kv_block}",
                    block_index=(query_block, kv_block),
                    case_id=case.case_id,
                )
                merged_sum = previous_state[1]
            assert previous_state is not None
            merged_max, merged_sum, merged_acc = previous_state
            direct_scores = np.concatenate(score_blocks, axis=-1)
            with np.errstate(over="ignore", invalid="ignore", under="ignore"):
                direct_max = np.max(direct_scores, axis=-1)
                direct_sum = np.sum(
                    np.exp(direct_scores - direct_max[..., None]),
                    axis=-1,
                )
            if not np.allclose(
                merged_sum,
                direct_sum,
                rtol=self.softmax.rtol,
                atol=self.softmax.atol,
                equal_nan=False,
            ):
                raise BusinessError(
                    Issue(
                        ErrorCode.INCONSISTENT,
                        "merged chunk softmax denominator does not match the "
                        "full-sequence denominator",
                        case_id=case.case_id,
                        block_index=query_block,
                        category="softmax_merge_mismatch",
                        details={
                            "max_abs_delta": float(
                                np.max(np.abs(merged_sum - direct_sum))
                            )
                        },
                    )
                )
            block_out = merged_acc / merged_sum[..., None]
            blocks.append(np.ascontiguousarray(block_out))
        return np.ascontiguousarray(np.concatenate(blocks, axis=1))

    def compute_batch(
        self,
        items: Sequence[tuple[AttentionCase, QKVArrays]],
    ) -> BatchResult:
        cases = [case for case, _ in items]
        arrays = [arrays for _, arrays in items]
        issues = validate_cases(cases, arrays)
        if issues:
            return BatchResult(ok=False, results=[], errors=issues)
        results = [
            self.compute(case, arrays)
            for case, arrays in items
        ]
        return BatchResult(
            ok=all(result.ok for result in results),
            results=results,
        )

    def diagnose_cache(self):
        return self.cache.diagnose()

    def invalidation_report(
        self,
        previous_case: AttentionCase,
        previous_arrays: QKVArrays,
        updated_case: AttentionCase,
        updated_arrays: QKVArrays,
        *,
        baseline_result: AttentionResult | None = None,
        incremental_result: IncrementalResult | None = None,
    ) -> dict[str, Any]:
        result = incremental_result or self.incremental_update(
            previous_case,
            previous_arrays,
            updated_case,
            updated_arrays,
            baseline_result=baseline_result,
            verify_self_check=False,
        )
        return {
            "baseline_case_id": result.baseline_case_id,
            "updated_case_id": result.updated_case_id,
            "changed_kv_blocks": list(result.changed_kv_blocks),
            "changed_q_blocks": list(result.changed_q_blocks),
            "structural_invalidation": result.structural_invalidation,
            "invalidation_roots": list(result.invalidation_roots),
            "affected_nodes": list(result.affected_nodes),
            "recomputed_nodes": list(result.recomputed_nodes),
            "reused_nodes": list(result.reused_nodes),
            "disjoint": not (
                set(result.recomputed_nodes) & set(result.reused_nodes)
            ),
            "affected_covered": set(result.affected_nodes).issubset(
                set(result.recomputed_nodes) | set(result.reused_nodes)
            ),
        }
