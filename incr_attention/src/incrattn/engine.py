"""Incremental chunked-attention orchestration engine."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import numpy as np

from .config import EngineConfig
from .dependency import DependencyGraph
from .errors import (
    E_RECOMPUTE_PENDING,
    EditRejectedError,
    IncrementalAttentionError,
    NonFiniteInputError,
)
from .injection import KernelInjector
from .kernel import attention_block
from .snapshot import load_snapshot, verify_snapshot_freshness, write_snapshot
from .util import array_fingerprint, digest_bytes, num_blocks


@dataclass
class BlockFailure:
    block_index: int
    stage: str
    code: str
    exception_type: str
    message: str
    injection_id: str | None = None

    def to_dict(self) -> dict:
        return {
            "block_index": self.block_index,
            "stage": self.stage,
            "code": self.code,
            "exception_type": self.exception_type,
            "message": self.message,
            "injection_id": self.injection_id,
        }


@dataclass
class EngineState:
    queries: np.ndarray
    keys: np.ndarray | None
    values: np.ndarray | None
    config: EngineConfig
    graph: DependencyGraph
    results: dict
    pending: list
    failures: dict
    global_version: int
    last_recovery_point: str
    edit_log: list
    input_fingerprint: str
    batches: list

    def scale_value(self) -> float:
        if self.config.scale is not None:
            return float(self.config.scale)
        return 1.0 / np.sqrt(self.queries.shape[1])


class IncrementalAttentionEngine:
    """Stateful engine: edits -> precise invalidation -> budgeted batches."""

    def __init__(self, config, queries, injection=None):
        config.validate(allow_zero_budget=injection is not None and bool(
            injection.enabled("zero_budget")
        ))
        queries = np.ascontiguousarray(queries, dtype=np.float32)
        self.state = EngineState(
            queries=queries,
            keys=None,
            values=None,
            config=config,
            graph=DependencyGraph.build(
                config.block_size, queries.shape[0], config.fingerprint()
            ),
            results={},
            pending=[],
            failures={},
            global_version=0,
            last_recovery_point="fresh-start",
            edit_log=[],
            input_fingerprint=self._input_fingerprint(queries, None, None),
            batches=[],
        )
        self.injection = injection

    # ------------------------------------------------------------------ #
    # Fingerprints
    # ------------------------------------------------------------------ #
    @staticmethod
    def _input_fingerprint(queries, keys, values) -> str:
        parts = [array_fingerprint(np.asarray(queries, dtype=np.float32))]
        if keys is not None:
            parts.append(array_fingerprint(np.asarray(keys, dtype=np.float32)))
            parts.append(array_fingerprint(np.asarray(values, dtype=np.float32)))
        else:
            parts.append("no-kv")
        return digest_bytes(parts)

    def _refresh_input_fingerprint(self) -> None:
        self.state.input_fingerprint = self._input_fingerprint(
            self.state.queries, self.state.keys, self.state.values
        )

    # ------------------------------------------------------------------ #
    # Edits and precise invalidation
    # ------------------------------------------------------------------ #
    def append_kv(self, key_block, value_block) -> list[int]:
        key_block = np.ascontiguousarray(key_block, dtype=np.float32)
        value_block = np.ascontiguousarray(value_block, dtype=np.float32)
        if key_block.shape != value_block.shape:
            raise EditRejectedError(
                f"K/V block shapes differ: {key_block.shape} vs {value_block.shape}"
            )
        if key_block.ndim != 2 or key_block.shape[1] != self.state.queries.shape[1]:
            raise EditRejectedError("K/V block head dimension does not match Q")
        n_q = self.state.queries.shape[0]
        if self.state.keys is not None and self.state.keys.shape[0] >= n_q:
            raise EditRejectedError("KV sequence already covers all query rows")
        if self.state.keys is None:
            new_keys = key_block.copy()
            new_values = value_block.copy()
            affected = (0, num_blocks(new_keys.shape[0], self.state.config.block_size))
        else:
            new_keys = np.concatenate([self.state.keys, key_block], axis=0)
            new_values = np.concatenate([self.state.values, value_block], axis=0)
            old_block_count = num_blocks(
                self.state.keys.shape[0], self.state.config.block_size
            )
            new_block_count = num_blocks(
                new_keys.shape[0], self.state.config.block_size
            )
            affected = (old_block_count, new_block_count)
        if new_keys.shape[0] > n_q:
            raise EditRejectedError("appended KV would exceed the query sequence length")
        self.state.keys = new_keys
        self.state.values = new_values
        self._refresh_input_fingerprint()
        invalidated = self.state.graph.invalidate_interval(*affected)
        self._enqueue(invalidated)
        self.state.edit_log.append(
            {"op": "append", "kv_block_range": list(affected), "rows": int(key_block.shape[0])}
        )
        return invalidated

    def replace_kv_block(self, kv_block_index, key_block, value_block) -> list[int]:
        key_block = np.ascontiguousarray(key_block, dtype=np.float32)
        value_block = np.ascontiguousarray(value_block, dtype=np.float32)
        if self.state.keys is None:
            raise EditRejectedError("cannot replace before any KV block exists")
        block_size = self.state.config.block_size
        start = kv_block_index * block_size
        end = min(self.state.keys.shape[0], start + block_size)
        if start >= self.state.keys.shape[0]:
            raise EditRejectedError(f"KV block {kv_block_index} does not exist")
        expected_rows = end - start
        if key_block.shape != (expected_rows, self.state.queries.shape[1]):
            raise EditRejectedError(
                f"replacement shape {key_block.shape} must match "
                f"({expected_rows}, {self.state.queries.shape[1]})"
            )
        self.state.keys[start:end] = key_block
        self.state.values[start:end] = value_block
        self._refresh_input_fingerprint()
        invalidated = self.state.graph.invalidate_interval(
            kv_block_index, kv_block_index + 1
        )
        self._enqueue(invalidated)
        self.state.edit_log.append(
            {"op": "replace", "kv_block_index": kv_block_index}
        )
        return invalidated

    def change_block_size(self, new_block_size) -> list[int]:
        """Rechunk: rebuild graph and accumulation chain; no old result reused."""
        if new_block_size <= 0:
            raise EditRejectedError("new block_size must be positive")
        if new_block_size == self.state.config.block_size:
            return []
        self.state.config = self.state.config.replace(block_size=new_block_size)
        self.state.graph = DependencyGraph.build(
            new_block_size,
            self.state.queries.shape[0],
            self.state.config.fingerprint(),
        )
        self.state.results = {}
        self.state.failures = {}
        invalidated = self.state.graph.invalidate_all()
        self.state.pending = sorted(invalidated)
        self.state.edit_log.append(
            {"op": "block_size_change", "block_size": new_block_size}
        )
        self._refresh_input_fingerprint()
        return invalidated

    def _enqueue(self, block_indices) -> None:
        queued = set(self.state.pending)
        for index in block_indices:
            queued.add(index)
            self.state.failures.pop(index, None)
        self.state.pending = sorted(queued)


    # ------------------------------------------------------------------ #
    # Budgeted batch execution
    # ------------------------------------------------------------------ #
    def run_batch(self) -> dict:
        state = self.state
        budget = state.config.recompute_budget
        eligible = list(state.pending)
        planned = eligible[:budget]
        remaining = eligible[budget:]
        planned_set = set(planned)

        reused = [
            node.block_index
            for node in state.graph.nodes
            if node.valid and node.block_index not in planned_set
        ]

        injector = KernelInjector(self.injection)

        def task(block_index):
            # Stable sort key is the block index itself; call sites always
            # consume outcomes in ascending block order, so thread scheduling
            # cannot change any observable ordering.
            try:
                result = attention_block(
                    block_index,
                    state.queries,
                    state.keys,
                    state.values,
                    state.config.block_size,
                    state.scale_value(),
                    injector=injector,
                )
                return block_index, result, None
            except IncrementalAttentionError as exc:
                injection_id = None
                if isinstance(exc, NonFiniteInputError):
                    item = (
                        self.injection.first("nonfinite_input")
                        if self.injection is not None
                        else None
                    )
                    if item is not None:
                        injection_id = item["id"]
                        injector.fired.append(injection_id)
                else:
                    injection_id = injector.fired_by_block.get(block_index)
                failure = BlockFailure(
                    block_index=block_index,
                    stage=getattr(exc, "stage", "input"),
                    code=exc.code,
                    exception_type=type(exc).__name__,
                    message=str(exc),
                    injection_id=injection_id,
                )
                return block_index, None, failure
            except Exception as exc:  # defensive: still isolate the block
                failure = BlockFailure(
                    block_index=block_index,
                    stage="unknown",
                    code="E_KERNEL_UNKNOWN",
                    exception_type=type(exc).__name__,
                    message=str(exc),
                )
                return block_index, None, failure

        ordered_planned = sorted(planned, key=lambda index: index)
        if state.config.max_workers == 1 or len(ordered_planned) <= 1:
            outcomes = [task(index) for index in ordered_planned]
        else:
            with ThreadPoolExecutor(max_workers=state.config.max_workers) as pool:
                futures = [pool.submit(task, index) for index in ordered_planned]
                outcomes = [future.result() for future in futures]

        # Re-sort by the stable key so completion, failure and budget-deduction
        # sequences are identical to a single-threaded run.
        outcomes.sort(key=lambda item: item[0])
        completed, failures = [], {}
        for block_index, result, failure in outcomes:
            if failure is None:
                state.failures.pop(block_index, None)
                state.results[block_index] = result
                state.graph.mark_valid(block_index, result.fingerprint)
                completed.append(block_index)
            else:
                if failure.injection_id is None:
                    failure.injection_id = injector.fired_by_block.get(block_index)
                failures[block_index] = failure
                state.failures[block_index] = failure
                node = state.graph.nodes[block_index]
                node.valid = False
                node.output_fingerprint = None

        # Failed blocks stay at the front of the queue; earlier invalid blocks
        # are never skipped to fill the budget.
        state.pending = sorted(set(remaining) | set(failures))
        state.global_version += 1
        state.last_recovery_point = (
            f"version={state.global_version}:"
            f"{os.path.basename(state.config.snapshot_path)}"
        )
        snapshot_path = self._write_snapshot()

        report = {
            "batch": len(state.batches) + 1,
            "global_version": state.global_version,
            "planned": ordered_planned,
            "completed": sorted(completed),
            "reused": sorted(reused),
            "invalidated_this_batch": ordered_planned,
            "pending": [
                {"block_index": index, "code": E_RECOMPUTE_PENDING}
                for index in state.pending
            ],
            "failures": [
                failures[index].to_dict() for index in sorted(failures)
            ],
            "injections_fired": sorted(set(injector.fired)),
            "budget_configured": budget,
            "budget_consumed": len(planned),
            "budget_remaining": max(0, budget - len(planned)),
            "snapshot_path": snapshot_path,
            "recovery_point": state.last_recovery_point,
            "converged": not state.pending and not state.failures,
        }
        state.batches.append(report)
        return report

    def run_to_convergence(self, max_batches: int = 1000) -> list[dict]:
        reports = []
        for _ in range(max_batches):
            report = self.run_batch()
            reports.append(report)
            if report["converged"]:
                return reports
        raise RuntimeError("engine did not converge within the batch limit")

    # ------------------------------------------------------------------ #
    # Snapshot persistence and recovery
    # ------------------------------------------------------------------ #
    def _write_snapshot(self) -> str:
        state = self.state
        n_q = state.queries.shape[0]
        n_blocks = num_blocks(n_q, state.config.block_size)
        head_dim = state.queries.shape[1]
        row_max = np.zeros((n_blocks, state.config.block_size), dtype=np.float32)
        exp_sum = np.zeros_like(row_max)
        output = np.zeros(
            (n_blocks, state.config.block_size, head_dim), dtype=np.float32
        )
        fingerprints = np.full(n_blocks, "", dtype=np.dtype("U64"))
        valid_flags = np.zeros(n_blocks, dtype=np.uint8)
        kv_lo = np.zeros(n_blocks, dtype=np.int64)
        kv_hi = np.zeros(n_blocks, dtype=np.int64)
        completed = []
        for index, node in enumerate(state.graph.nodes):
            kv_lo[index], kv_hi[index] = node.kv_block_range
            if node.valid and index in state.results:
                result = state.results[index]
                rows = result.row_max.shape[0]
                row_max[index, :rows] = result.row_max
                exp_sum[index, :rows] = result.exp_sum
                output[index, :rows, :] = result.output
                fingerprints[index] = result.fingerprint
                valid_flags[index] = 1
                completed.append(index)
        kv_length = 0 if state.keys is None else state.keys.shape[0]
        metadata = {
            "global_version": state.global_version,
            "config_fingerprint": state.config.fingerprint(),
            "input_fingerprint": state.input_fingerprint,
            "block_size": state.config.block_size,
            "query_length": int(n_q),
            "kv_length": int(kv_length),
            "head_dim": int(head_dim),
            "completed_blocks": sorted(completed),
            "pending_blocks": list(state.pending),
            "recovery_point": state.last_recovery_point,
            "edit_log": state.edit_log,
        }
        arrays = {
            "queries": state.queries,
            "keys": (
                state.keys
                if state.keys is not None
                else np.zeros((0, head_dim), dtype=np.float32)
            ),
            "values": (
                state.values
                if state.values is not None
                else np.zeros((0, head_dim), dtype=np.float32)
            ),
            "row_max": row_max,
            "exp_sum": exp_sum,
            "output": output,
            "fingerprints": fingerprints.astype(np.dtype("U64")),
            "valid_flags": valid_flags,
            "kv_lo": kv_lo,
            "kv_hi": kv_hi,
        }
        write_snapshot(state.config.snapshot_path, {
            "metadata": metadata,
            "arrays": arrays,
        })
        return state.config.snapshot_path

    @classmethod
    def restore(cls, config, injection=None):
        """Resume only from a fully replaced, fingerprint-matching snapshot."""
        snapshot = load_snapshot(config.snapshot_path)
        arrays = snapshot["arrays"]
        queries = np.ascontiguousarray(arrays["queries"], dtype=np.float32)
        keys = arrays["keys"]
        values = arrays["values"]
        expected_input = cls._input_fingerprint(
            queries,
            None if keys.shape[0] == 0 else keys,
            None if values.shape[0] == 0 else values,
        )
        verify_snapshot_freshness(
            snapshot, config.fingerprint(), expected_input
        )
        metadata = snapshot["metadata"]
        engine = cls(config, queries, injection=injection)
        state = engine.state
        if keys.shape[0]:
            state.keys = np.ascontiguousarray(keys, dtype=np.float32)
            state.values = np.ascontiguousarray(values, dtype=np.float32)
        state.input_fingerprint = expected_input
        state.global_version = int(metadata["global_version"])
        state.pending = [int(index) for index in metadata["pending_blocks"]]
        state.edit_log = list(metadata.get("edit_log", []))
        state.last_recovery_point = metadata["recovery_point"]
        for index, node in enumerate(state.graph.nodes):
            if arrays["valid_flags"][index] == 1:
                start = index * config.block_size
                end = min(queries.shape[0], start + config.block_size)
                rows = end - start
                from .kernel import BlockResult

                result = BlockResult(
                    block_index=index,
                    row_max=arrays["row_max"][index, :rows].copy(),
                    exp_sum=arrays["exp_sum"][index, :rows].copy(),
                    output=arrays["output"][index, :rows, :].copy(),
                    kv_block_range=(
                        int(arrays["kv_lo"][index]),
                        int(arrays["kv_hi"][index]),
                    ),
                    fingerprint=str(arrays["fingerprints"][index]),
                )
                state.results[index] = result
                state.graph.mark_valid(index, result.fingerprint)
        return engine

    # ------------------------------------------------------------------ #
    # One-shot full recompute used for equivalence acceptance
    # ------------------------------------------------------------------ #
    def full_recompute(self):
        state = self.state
        return [
            attention_block(
                index,
                state.queries,
                state.keys,
                state.values,
                state.config.block_size,
                state.scale_value(),
            )
            for index in range(
                num_blocks(state.queries.shape[0], state.config.block_size)
            )
        ]

    def block_results(self) -> list:
        return [
            state_result
            for index, state_result in sorted(self.state.results.items())
        ]
