"""Integrated RoPE attention engine with cache-aware incremental recomputation."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable, Iterable

import numpy as np

from .attention import (
    AttentionComparison,
    BlockedAttentionResult,
    NumericTolerance,
    OnlineSoftmaxBlock,
    compare_blocked_to_full,
    compute_score_block,
    full_attention,
    key_prefix_length,
    merge_softmax_stats,
    online_softmax,
    query_blocks,
)
from .cache import CacheStore, NodeCacheResult
from .data import AttentionTask, AttentionTensors
from .errors import (
    E_INCONSISTENT,
    E_RESOURCE_LIMIT,
    RopeError,
)
from .models import ParsedConfig, ResourceLimits
from .rope import (
    ROPE_PAIRING,
    compute_position_angles,
    resolve_head_scales,
    rotate_qk,
    rotation_summary,
    validate_config_for_task,
)
from .snapshot import ConfigSnapshot, SnapshotStore
from .util import hash_array, sha256_json


@dataclass(frozen=True)
class CaseResult:
    case_id: str
    config_version: str
    output: np.ndarray
    output_hash: str
    output_byte_length: int
    angles_hash: str
    q_rot_hash: str
    k_rot_hash: str
    rotation_summary: dict[str, Any]
    attention_comparison: AttentionComparison | None
    moved_memory_bytes: int
    node_events: tuple[NodeCacheResult, ...]
    business_codes: tuple[str, ...]
    degradations: tuple[dict[str, Any], ...]

    def byte_view(self) -> bytes:
        return np.ascontiguousarray(self.output).tobytes(order="C")

    def to_dict(self) -> dict[str, Any]:
        return {
            "angles_hash": self.angles_hash,
            "attention_comparison": (
                self.attention_comparison.to_dict()
                if self.attention_comparison is not None
                else None
            ),
            "business_codes": list(self.business_codes),
            "case_id": self.case_id,
            "config_version": self.config_version,
            "degradations": list(self.degradations),
            "k_rot_hash": self.k_rot_hash,
            "moved_memory_bytes": self.moved_memory_bytes,
            "node_events": [event.to_dict() for event in self.node_events],
            "output_byte_length": self.output_byte_length,
            "output_hash": self.output_hash,
            "q_rot_hash": self.q_rot_hash,
            "rotation_summary": self.rotation_summary,
        }


@dataclass(frozen=True)
class EngineResult:
    snapshot_version: int
    snapshot_checksum: str
    config_version: str
    cases: dict[str, CaseResult]
    recomputed_nodes: tuple[str, ...]
    reused_nodes: tuple[str, ...]
    affected_cases: tuple[str, ...]
    business_codes: tuple[str, ...]
    degradations: tuple[dict[str, Any], ...]
    cache_diagnostics: dict[str, Any]
    parallel_nodes_requested: int
    parallel_nodes_effective: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "affected_cases": list(self.affected_cases),
            "business_codes": list(self.business_codes),
            "cache_diagnostics": self.cache_diagnostics,
            "cases": {
                case_id: self.cases[case_id].to_dict()
                for case_id in sorted(self.cases)
            },
            "config_version": self.config_version,
            "degradations": list(self.degradations),
            "parallel_nodes_effective": self.parallel_nodes_effective,
            "parallel_nodes_requested": self.parallel_nodes_requested,
            "recomputed_nodes": list(self.recomputed_nodes),
            "reused_nodes": list(self.reused_nodes),
            "snapshot_checksum": self.snapshot_checksum,
            "snapshot_version": self.snapshot_version,
        }


def retarget_tasks(
    tasks: Iterable[AttentionTask],
    config: ParsedConfig,
) -> list[AttentionTask]:
    """Move task records onto a new config version without changing Q/K/V."""

    return [
        AttentionTask(
            case_id=task.case_id,
            seq_len=task.seq_len,
            head_dim=task.head_dim,
            num_heads=task.num_heads,
            base_theta=config.base_theta,
            pos_encoding_variant=config.effective_variant,
            pos_scale=config.pos_scale,
            block_size=config.block_size,
            causal=task.causal,
            seed=task.seed,
            config_version=config.source_version,
        )
        for task in sorted(tasks, key=lambda item: item.case_id)
    ]


def estimate_block_memory(task: AttentionTask, block_size: int) -> int:
    """Conservative peak for score and online-accumulator chunks in one call."""

    peak = 0
    for q_start, q_end in query_blocks(task.seq_len, block_size):
        key_end = key_prefix_length(q_end, task.seq_len, task.causal)
        q_len = q_end - q_start
        score_bytes = task.num_heads * q_len * key_end * 8
        output_bytes = task.num_heads * q_len * task.head_dim * 8
        # scores + shifted probabilities + weighted block + accumulators
        peak = max(peak, 3 * score_bytes + 3 * output_bytes)
    return int(peak)


def estimate_full_reference_memory(task: AttentionTask) -> int:
    scores = task.num_heads * task.seq_len * task.seq_len * 8
    output = task.num_heads * task.seq_len * task.head_dim * 8
    return int(3 * scores + 2 * output)


def _base_key_parts(
    task: AttentionTask,
    config: ParsedConfig,
    snapshot: ConfigSnapshot,
) -> dict[str, Any]:
    return {
        "base_theta": config.base_theta,
        "block_size": config.block_size,
        "case_signature": {
            "case_id": task.case_id,
            "causal": task.causal,
            "head_dim": task.head_dim,
            "num_heads": task.num_heads,
            "seq_len": task.seq_len,
        },
        "causal": task.causal,
        "config_checksum": config.content_checksum(),
        "config_version": config.source_version,
        "frequency_adjustment": config.effective_frequency_adjustment.to_dict(),
        "per_head_scale": list(config.effective_per_head_scale),
        "pos_scale": config.pos_scale,
        "snapshot_checksum": snapshot.checksum,
        "snapshot_version": snapshot.version,
        "variant": config.effective_variant,
    }


def _node_key_parts(
    base: dict[str, Any],
    *,
    block_content_hash: str,
    upstream_node_hash: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    value = dict(base)
    value["block_content_hash"] = block_content_hash
    value["upstream_node_hash"] = upstream_node_hash
    if details:
        value["node_details"] = details
    return value


def _semantic_fingerprint(node: str, value: dict[str, Any]) -> str:
    return sha256_json({"algorithm": "rope-attention/v1", "node": node, "semantics": value})


def _block_hashes(array: np.ndarray, block_size: int) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for index, (start, end) in enumerate(query_blocks(array.shape[1], block_size)):
        result.append(
            {
                "block_index": index,
                "end": end,
                "hash": hash_array(array[:, start:end, :]),
                "start": start,
            }
        )
    return result


def _stats_from_payload(
    arrays: dict[str, np.ndarray],
    metadata: dict[str, Any],
) -> tuple[dict[str, Any], ...]:
    stats: list[dict[str, Any]] = []
    for item in metadata["block_stats"]:
        index = int(item["block_index"])
        stats.append(
            {
                "block_index": index,
                "key_end": int(item["key_end"]),
                "key_start": int(item["key_start"]),
                "max": np.ascontiguousarray(
                    arrays[f"block_max_{index}"], dtype=np.float64
                ),
                "sum": np.ascontiguousarray(
                    arrays[f"block_sum_{index}"], dtype=np.float64
                ),
            }
        )
    return tuple(stats)


class AttentionEngine:
    """Integrable facade for config-bound deterministic attention computation."""

    def __init__(
        self,
        cache_store: CacheStore | None,
        *,
        tolerance: NumericTolerance | None = None,
        limits: ResourceLimits | None = None,
        snapshot_store: SnapshotStore | None = None,
        use_cache: bool = True,
        validate_reference: bool = True,
        allow_semantic_alias: bool = True,
    ) -> None:
        self.cache_store = cache_store
        self.tolerance = tolerance or NumericTolerance()
        self.limits = limits or ResourceLimits()
        self.snapshot_store = snapshot_store
        self.use_cache = use_cache and cache_store is not None
        self.validate_reference = validate_reference
        self.allow_semantic_alias = allow_semantic_alias

    def _run_node(
        self,
        *,
        node: str,
        case_id: str,
        key_parts: dict[str, Any],
        semantic_parts: dict[str, Any],
        compute: Callable[[], tuple[dict[str, np.ndarray], dict[str, Any]]],
    ) -> NodeCacheResult:
        semantic = _semantic_fingerprint(node, semantic_parts)
        if not self.use_cache:
            arrays, metadata = compute()
            from .cache import encode_payload

            payload = encode_payload(arrays, metadata)
            key = (
                self.cache_store.key_for(node, key_parts)
                if self.cache_store is not None
                else sha256_json({"node": node, "parts": key_parts})
            )
            return NodeCacheResult(
                node=node,
                case_id=case_id,
                key=key,
                key_parts=key_parts,
                semantic_fingerprint=semantic,
                cache_version=None,
                entry_checksum=None,
                payload_hash=sha256_json(payload),
                arrays=arrays,
                metadata=metadata,
                status="computed_uncached",
            )
        assert self.cache_store is not None
        return self.cache_store.get_or_compute(
            node=node,
            case_id=case_id,
            key_parts=key_parts,
            semantic_fingerprint=semantic,
            compute=compute,
            max_entry_count=self.limits.max_cache_entries,
            overflow_policy=self.limits.cache_overflow_policy,
            allow_semantic_alias=self.allow_semantic_alias,
        )

    def compute_case(
        self,
        task: AttentionTask,
        tensors: AttentionTensors,
        snapshot: ConfigSnapshot,
    ) -> CaseResult:
        config = snapshot.config
        validate_config_for_task(task, config)
        moved_memory = estimate_block_memory(task, config.block_size)
        if moved_memory > self.limits.max_block_memory_bytes:
            raise RopeError(
                E_RESOURCE_LIMIT,
                "estimated block working set exceeds the configured limit",
                details={
                    "case_id": task.case_id,
                    "estimated_bytes": moved_memory,
                    "max_block_memory_bytes": self.limits.max_block_memory_bytes,
                },
            )

        base = _base_key_parts(task, config, snapshot)
        events: list[NodeCacheResult] = []
        degradations: list[dict[str, Any]] = []
        business_codes: list[str] = []
        q_hash = hash_array(tensors.q)
        k_hash = hash_array(tensors.k)
        v_hash = hash_array(tensors.v)
        v_blocks = _block_hashes(tensors.v, config.block_size)
        head_scales = resolve_head_scales(
            config, task.num_heads, case_id=task.case_id
        )

        position_parts = _node_key_parts(
            base,
            block_content_hash=hash_array(head_scales),
            upstream_node_hash=snapshot.checksum,
            details={"operation": "position_angles", "pairing": ROPE_PAIRING},
        )
        position_event = self._run_node(
            node="position_angles",
            case_id=task.case_id,
            key_parts=position_parts,
            semantic_parts={
                "base_theta": config.base_theta,
                "case": base["case_signature"],
                "frequency_adjustment": config.effective_frequency_adjustment.to_dict(),
                "head_scales": [float(value) for value in head_scales],
                "pairing": ROPE_PAIRING,
                "pos_scale": config.pos_scale,
                "variant": config.effective_variant,
            },
            compute=lambda: (
                {"angles": compute_position_angles(task, config)},
                {
                    "pairing": ROPE_PAIRING,
                    "shape": [task.num_heads, task.seq_len, task.head_dim // 2],
                },
            ),
        )
        events.append(position_event)
        angles = position_event.arrays["angles"]
        angles_hash = hash_array(angles)

        rotation_parts = _node_key_parts(
            base,
            block_content_hash=sha256_json({"k": k_hash, "q": q_hash}),
            upstream_node_hash=angles_hash,
            details={"operation": "qk_rotation"},
        )
        rotation_event = self._run_node(
            node="qk_rotation",
            case_id=task.case_id,
            key_parts=rotation_parts,
            semantic_parts={
                "angles_hash": angles_hash,
                "k_hash": k_hash,
                "pairing": ROPE_PAIRING,
                "q_hash": q_hash,
            },
            compute=lambda: (
                dict(
                    zip(
                        ("q_rot", "k_rot"),
                        rotate_qk(tensors.q, tensors.k, angles),
                    )
                ),
                {"pairing": ROPE_PAIRING, "shape": list(tensors.q.shape)},
            ),
        )
        events.append(rotation_event)
        q_rot = rotation_event.arrays["q_rot"]
        k_rot = rotation_event.arrays["k_rot"]
        q_rot_hash = hash_array(q_rot)
        k_rot_hash = hash_array(k_rot)
        rotation_event_hash = rotation_event.payload_hash

        block_outputs: list[np.ndarray] = []
        block_max: list[np.ndarray] = []
        block_sum: list[np.ndarray] = []
        block_stats: list[tuple[dict[str, Any], ...]] = []
        score_events: list[NodeCacheResult] = []
        for block_index, (q_start, q_end) in enumerate(
            query_blocks(task.seq_len, config.block_size)
        ):
            key_end = key_prefix_length(q_end, task.seq_len, task.causal)
            q_block_hash = hash_array(q_rot[:, q_start:q_end, :])
            k_blocks = [
                {
                    "block_index": index,
                    "end": end,
                    "hash": hash_array(k_rot[:, start:end, :]),
                    "start": start,
                }
                for index, (start, end) in enumerate(
                    query_blocks(key_end, config.block_size)
                )
            ]
            score_content_hash = sha256_json(
                {
                    "causal": task.causal,
                    "key_blocks": k_blocks,
                    "q_block_hash": q_block_hash,
                    "q_end": q_end,
                    "q_start": q_start,
                }
            )
            score_parts = _node_key_parts(
                base,
                block_content_hash=score_content_hash,
                upstream_node_hash=rotation_event_hash,
                details={"block_index": block_index, "operation": "block_scores"},
            )
            score_event = self._run_node(
                node=f"block_scores[{block_index:04d}]",
                case_id=task.case_id,
                key_parts=score_parts,
                semantic_parts={
                    "block_index": block_index,
                    "block_size": config.block_size,
                    "causal": task.causal,
                    "head_dim": task.head_dim,
                    "k_blocks": k_blocks,
                    "key_end": key_end,
                    "q_block_hash": q_block_hash,
                    "q_end": q_end,
                    "q_start": q_start,
                },
                compute=lambda q_start=q_start, q_end=q_end, key_end=key_end: (
                    {
                        "scores": compute_score_block(
                            q_rot,
                            k_rot,
                            q_start=q_start,
                            q_end=q_end,
                            key_end=key_end,
                            causal=task.causal,
                            head_dim=task.head_dim,
                        )
                    },
                    {
                        "causal": task.causal,
                        "key_end": key_end,
                        "q_end": q_end,
                        "q_start": q_start,
                        "shape": [
                            task.num_heads,
                            q_end - q_start,
                            key_end,
                        ],
                    },
                ),
            )
            events.append(score_event)
            score_events.append(score_event)
            scores = score_event.arrays["scores"]
            v_prefix_blocks = [
                block for block in v_blocks if int(block["start"]) < key_end
            ]
            online_parts = _node_key_parts(
                base,
                block_content_hash=sha256_json(
                    {
                        "score_payload_hash": score_event.payload_hash,
                        "v_blocks": v_prefix_blocks,
                    }
                ),
                upstream_node_hash=score_event.payload_hash,
                details={"block_index": block_index, "operation": "online_softmax"},
            )

            def _online(
                scores: np.ndarray = scores,
                key_end: int = key_end,
                q_start: int = q_start,
            ) -> tuple[dict[str, np.ndarray], dict[str, Any]]:
                online = online_softmax(
                    scores,
                    tensors.v[:, :key_end, :],
                    block_size=config.block_size,
                    causal=task.causal,
                    q_start=q_start,
                )
                arrays = {
                    "output": online.output,
                    "final_max": online.final_max,
                    "final_sum": online.final_sum,
                }
                block_metadata: list[dict[str, Any]] = []
                for stat in online.block_stats:
                    index = int(stat["block_index"])
                    arrays[f"block_max_{index}"] = stat["max"]
                    arrays[f"block_sum_{index}"] = stat["sum"]
                    block_metadata.append(
                        {
                            "block_index": index,
                            "key_end": int(stat["key_end"]),
                            "key_start": int(stat["key_start"]),
                        }
                    )
                return arrays, {
                    "block_stats": block_metadata,
                    "causal": task.causal,
                    "q_end": q_end,
                    "q_start": q_start,
                }

            online_event = self._run_node(
                node=f"online_softmax[{block_index:04d}]",
                case_id=task.case_id,
                key_parts=online_parts,
                semantic_parts={
                    "block_index": block_index,
                    "block_size": config.block_size,
                    "causal": task.causal,
                    "q_end": q_end,
                    "q_start": q_start,
                    "score_payload_hash": score_event.payload_hash,
                    "v_blocks": v_prefix_blocks,
                },
                compute=_online,
            )
            events.append(online_event)
            block_outputs.append(online_event.arrays["output"])
            block_max.append(online_event.arrays["final_max"])
            block_sum.append(online_event.arrays["final_sum"])
            block_stats.append(
                _stats_from_payload(online_event.arrays, online_event.metadata)
            )

        online_events = [event for event in events if event.node.startswith("online_softmax[")]
        output_parts = _node_key_parts(
            base,
            block_content_hash=sha256_json(
                {
                    "block_output_hashes": [
                        event.payload_hash for event in online_events
                    ],
                    "v_hash": v_hash,
                }
            ),
            upstream_node_hash=sha256_json(
                [event.payload_hash for event in online_events]
            ),
            details={"operation": "output_accum"},
        )
        output_event = self._run_node(
            node="output_accum",
            case_id=task.case_id,
            key_parts=output_parts,
            semantic_parts={
                "block_output_hashes": [
                    event.payload_hash for event in online_events
                ],
                "block_size": config.block_size,
                "causal": task.causal,
            },
            compute=lambda: (
                {"output": np.ascontiguousarray(
                    np.concatenate(block_outputs, axis=1), dtype=np.float64
                )},
                {
                    "block_count": len(block_outputs),
                    "shape": [task.num_heads, task.seq_len, task.head_dim],
                },
            ),
        )
        events.append(output_event)
        for event in events:
            if event.degradation is not None:
                degradations.append(
                    {
                        "case_id": task.case_id,
                        "node": event.node,
                        **event.degradation,
                    }
                )
        output = output_event.arrays["output"]
        expected_output = np.concatenate(block_outputs, axis=1)
        if not _bytes_equal(output, expected_output):
            raise RopeError(
                E_INCONSISTENT,
                "cached output accumulator differs from replayed block outputs",
                details={"case_id": task.case_id},
            )

        comparison: AttentionComparison | None = None
        if self.validate_reference:
            reference_memory = estimate_full_reference_memory(task)
            if reference_memory > self.limits.max_block_memory_bytes:
                degradations.append(
                    {
                        "case_id": task.case_id,
                        "category": "reference_validation_skipped",
                        "estimated_bytes": reference_memory,
                        "max_block_memory_bytes": self.limits.max_block_memory_bytes,
                    }
                )
                business_codes.append("OK_DEGRADED")
            else:
                full = full_attention(
                    q_rot,
                    k_rot,
                    tensors.v,
                    causal=task.causal,
                    head_dim=task.head_dim,
                )
                blocked = BlockedAttentionResult(
                    output=np.ascontiguousarray(output, dtype=np.float64),
                    block_outputs=tuple(block_outputs),
                    block_max=tuple(block_max),
                    block_sum=tuple(block_sum),
                    block_stats=tuple(block_stats),
                )
                comparison = compare_blocked_to_full(
                    blocked, full, tolerance=self.tolerance
                )

        if degradations and "OK_DEGRADED" not in business_codes:
            business_codes.append("OK_DEGRADED")
        if not business_codes:
            business_codes.append("OK")
        return CaseResult(
            case_id=task.case_id,
            config_version=config.source_version,
            output=output,
            output_hash=hash_array(output),
            output_byte_length=int(output.nbytes),
            angles_hash=angles_hash,
            q_rot_hash=q_rot_hash,
            k_rot_hash=k_rot_hash,
            rotation_summary=rotation_summary(angles),
            attention_comparison=comparison,
            moved_memory_bytes=moved_memory,
            node_events=tuple(events),
            business_codes=tuple(business_codes),
            degradations=tuple(degradations),
        )

    def compute_all(
        self,
        tasks: Iterable[AttentionTask],
        tensors_by_case: dict[str, AttentionTensors],
        snapshot: ConfigSnapshot,
        *,
        parallel_nodes: int = 1,
    ) -> EngineResult:
        ordered_tasks = sorted(tasks, key=lambda item: item.case_id)
        if self.snapshot_store is not None:
            snapshot = self.snapshot_store.ensure_current(snapshot)
        degradations: list[dict[str, Any]] = []
        requested = int(parallel_nodes)
        if requested <= 0:
            raise ValueError("parallel_nodes must be positive")
        effective = requested
        if requested > self.limits.max_parallel_recompute_nodes:
            if self.limits.parallelism_overflow_policy == "error":
                raise RopeError(
                    E_RESOURCE_LIMIT,
                    "parallel node limit exceeded",
                    details={
                        "requested": requested,
                        "max_parallel_recompute_nodes": self.limits.max_parallel_recompute_nodes,
                    },
                )
            effective = self.limits.max_parallel_recompute_nodes
            degradations.append(
                {
                    "category": "parallelism_limited",
                    "requested": requested,
                    "effective": effective,
                }
            )
        effective = min(effective, max(1, len(ordered_tasks)))

        def run(task: AttentionTask) -> CaseResult:
            if task.case_id not in tensors_by_case:
                raise RopeError(
                    E_INCONSISTENT,
                    "Q/K/V tensors are missing for a task",
                    details={"case_id": task.case_id},
                )
            return self.compute_case(task, tensors_by_case[task.case_id], snapshot)

        if effective == 1:
            case_results = [run(task) for task in ordered_tasks]
        else:
            with ThreadPoolExecutor(max_workers=effective) as executor:
                case_results = list(executor.map(run, ordered_tasks))

        cases = {result.case_id: result for result in case_results}
        events = [
            event
            for result in case_results
            for event in result.node_events
        ]
        recomputed = tuple(
            sorted(
                f"{event.case_id}:{event.node}"
                for event in events
                if event.computed
            )
        )
        reused = tuple(
            sorted(
                f"{event.case_id}:{event.node}"
                for event in events
                if event.reused
            )
        )
        affected_cases = tuple(
            sorted(
                {
                    event.case_id
                    for event in events
                    if event.computed
                }
            )
        )
        all_degradations = list(degradations)
        business_codes: list[str] = []
        for result in case_results:
            all_degradations.extend(
                {"case_id": result.case_id, **item}
                for item in result.degradations
            )
            business_codes.extend(result.business_codes)
        if all_degradations:
            business_codes.append("OK_DEGRADED")
        if not business_codes:
            business_codes.append("OK")
        diagnostics = (
            self.cache_store.diagnostics()
            if self.cache_store is not None
            else {"enabled": False}
        )
        if not self.use_cache:
            diagnostics["cache_enabled_for_compute"] = False
        return EngineResult(
            snapshot_version=snapshot.version,
            snapshot_checksum=snapshot.checksum,
            config_version=snapshot.config.source_version,
            cases=cases,
            recomputed_nodes=recomputed,
            reused_nodes=reused,
            affected_cases=affected_cases,
            business_codes=tuple(sorted(set(business_codes))),
            degradations=tuple(all_degradations),
            cache_diagnostics=diagnostics,
            parallel_nodes_requested=requested,
            parallel_nodes_effective=effective,
        )


def _bytes_equal(left: np.ndarray, right: np.ndarray) -> bool:
    left_array = np.ascontiguousarray(left)
    right_array = np.ascontiguousarray(right)
    return (
        left_array.dtype == right_array.dtype
        and left_array.shape == right_array.shape
        and left_array.tobytes(order="C") == right_array.tobytes(order="C")
    )


def assert_results_byte_identical(
    incremental: EngineResult,
    full: EngineResult,
    *,
    case_ids: Iterable[str] | None = None,
) -> dict[str, Any]:
    wanted = sorted(case_ids) if case_ids is not None else sorted(incremental.cases)
    hashes: dict[str, Any] = {}
    for case_id in wanted:
        if case_id not in incremental.cases or case_id not in full.cases:
            raise RopeError(
                E_INCONSISTENT,
                "cannot compare missing case results",
                details={"case_id": case_id},
            )
        left = incremental.cases[case_id]
        right = full.cases[case_id]
        identical = _bytes_equal(left.output, right.output)
        hashes[case_id] = {
            "byte_identical": identical,
            "full_hash": right.output_hash,
            "incremental_hash": left.output_hash,
        }
        if not identical:
            raise RopeError(
                E_INCONSISTENT,
                "incremental and full recomputation outputs differ",
                details={"case_id": case_id, **hashes[case_id]},
            )
    return {
        "all_byte_identical": all(
            item["byte_identical"] for item in hashes.values()
        ),
        "cases": hashes,
    }


def plan_invalidation(
    tasks: Iterable[AttentionTask],
    old_snapshot: ConfigSnapshot,
    new_snapshot: ConfigSnapshot,
) -> dict[str, Any]:
    """Static dependency plan; runtime cache events remain the source of truth."""

    old = old_snapshot.config
    new = new_snapshot.config
    position_changed = any(
        (
            old.base_theta != new.base_theta,
            old.pos_scale != new.pos_scale,
            old.effective_variant != new.effective_variant,
            old.effective_frequency_adjustment.to_dict()
            != new.effective_frequency_adjustment.to_dict(),
            list(old.effective_per_head_scale)
            != list(new.effective_per_head_scale),
        )
    )
    block_changed = old.block_size != new.block_size
    version_changed = (
        old_snapshot.version != new_snapshot.version
        or old.source_version != new.source_version
    )
    if position_changed:
        recompute_template = [
            "position_angles",
            "qk_rotation",
            "block_scores[*]",
            "online_softmax[*]",
            "output_accum",
        ]
        reuse_template: list[str] = []
        reason = "base_theta, pos_scale, variant, frequency, or head scale changed"
    elif block_changed:
        recompute_template = [
            "block_scores[*]",
            "online_softmax[*]",
            "output_accum",
        ]
        reuse_template = ["position_angles", "qk_rotation"]
        reason = "block_size changed; position and rotation semantics are unchanged"
    elif version_changed:
        recompute_template = []
        reuse_template = [
            "position_angles",
            "qk_rotation",
            "block_scores[*]",
            "online_softmax[*]",
            "output_accum",
        ]
        reason = "config version/snapshot changed without semantic change; semantic alias reuse expected"
    else:
        recompute_template = []
        reuse_template = [
            "position_angles",
            "qk_rotation",
            "block_scores[*]",
            "online_softmax[*]",
            "output_accum",
        ]
        reason = "no semantic or version change"
    affected_cases = (
        sorted(task.case_id for task in tasks)
        if position_changed or block_changed or version_changed
        else []
    )
    return {
        "affected_cases": affected_cases,
        "block_size_changed": block_changed,
        "config_version_changed": old.source_version != new.source_version,
        "position_parameters_changed": position_changed,
        "predicted_recompute_nodes": recompute_template,
        "predicted_reuse_nodes": reuse_template,
        "reason": reason,
        "snapshot_version_changed": old_snapshot.version != new_snapshot.version,
    }

