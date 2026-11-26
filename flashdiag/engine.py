"""Incremental FlashAttention diagnostics engine."""

from __future__ import annotations

import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from .core import (
    NormBlock,
    ScoreBlock,
    assert_arrays_equal,
    compute_full_blockwise,
    compute_full_reference,
    compute_score_block,
    first_array_difference,
    merge_score_blocks,
    normalize_block,
)
from .errors import (
    DiagnosticError,
    E_INPUT_INVALID,
    E_LEDGER_INTEGRITY,
    E_SNAPSHOT_ROLLBACK,
    E_TOLERANCE_EXCEEDED,
)
from .hashing import array_sha256, sha256_json
from .ledger import LedgerSnapshot, NodeRecord, PlanRecord, SnapshotLedger, SnapshotRecord
from .models import (
    BlockPlan,
    BlockRange,
    CaseRunResult,
    ResourcePolicy,
    ResourceUsage,
    TaskSpec,
    VerificationResult,
    estimate_q_block_resident_bytes,
    make_case_cache_scope,
    select_parallel_workers,
)
from .planner import (
    PreparedTask,
    affected_q_blocks_from_hashes,
    array_set_from_qkv,
    changed_heads,
    prepare_manifest,
    prepare_arrays,
)
from .store import ArtifactStore, CachedArtifact


CACHE_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class ScoreSpec:
    plan: BlockPlan
    kv_block: BlockRange
    cache_key: str
    q_block_sha256: str
    k_block_sha256: str
    v_block_sha256: str

    @property
    def node_id(self) -> str:
        return f"score:h{self.plan.head}:q{self.plan.q_block}:k{self.kv_block.index}"


@dataclass
class _CacheTracker:
    hits: int = 0
    misses: int = 0
    block_reused: list[str] = field(default_factory=list)
    block_recomputed: list[str] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        if self.block_reused is None:
            self.block_reused = []
        if self.block_recomputed is None:
            self.block_recomputed = []

    def hit(self, node_id: str, block_node: bool) -> None:
        with self._lock:
            self.hits += 1
            if block_node:
                self.block_reused.append(node_id)

    def miss(self, node_id: str, block_node: bool) -> None:
        with self._lock:
            self.misses += 1
            if block_node:
                self.block_recomputed.append(node_id)

    def finish(self) -> tuple[list[str], list[str]]:
        return sorted(set(self.block_recomputed)), sorted(set(self.block_reused))


def _validation_cache_key(prepared: PreparedTask) -> str:
    return sha256_json(
        {
            "cache_schema_version": CACHE_SCHEMA_VERSION,
            "kind": "validation",
            "scope": make_case_cache_scope(prepared.task),
            "task": prepared.task.canonical(),
            "array_sha256": prepared.arrays.qkv_sha256,
            "shape": list(prepared.arrays.q.shape),
        }
    )


def _plan_cache_key(validation_key: str, prepared: PreparedTask) -> str:
    return sha256_json(
        {
            "cache_schema_version": CACHE_SCHEMA_VERSION,
            "kind": "plan",
            "scope": make_case_cache_scope(prepared.task),
            "upstream_validation": validation_key,
            "structural_sha256": prepared.structural_sha256,
        }
    )


def _score_specs(
    prepared: PreparedTask,
    plan_artifact: CachedArtifact,
) -> list[ScoreSpec]:
    specs: list[ScoreSpec] = []
    for plan in prepared.plans:
        for kv_block in plan.kv_blocks:
            q_sha = prepared.q_block_hashes[plan.head][plan.q_block]
            k_sha = prepared.validation_meta["kv_block_sha256"][str(plan.head)]["k"][
                kv_block.index
            ]
            v_sha = prepared.validation_meta["kv_block_sha256"][str(plan.head)]["v"][
                kv_block.index
            ]
            key = sha256_json(
                {
                    "cache_schema_version": CACHE_SCHEMA_VERSION,
                    "kind": "score",
                    "scope": make_case_cache_scope(prepared.task),
                    "upstream_plan": plan_artifact.content_sha256,
                    "head": plan.head,
                    "q_block": plan.q_block,
                    "kv_block": kv_block.index,
                    "q_start": plan.q_start,
                    "q_len": plan.q_len,
                    "kv_start": kv_block.start,
                    "kv_len": kv_block.length,
                    "q_block_sha256": q_sha,
                    "k_block_sha256": k_sha,
                    "v_block_sha256": v_sha,
                }
            )
            specs.append(
                ScoreSpec(
                    plan=plan,
                    kv_block=kv_block,
                    cache_key=key,
                    q_block_sha256=q_sha,
                    k_block_sha256=k_sha,
                    v_block_sha256=v_sha,
                )
            )
    return specs


def _norm_cache_key(
    prepared: PreparedTask,
    plan: BlockPlan,
    score_artifacts: Iterable[CachedArtifact],
) -> str:
    return sha256_json(
        {
            "cache_schema_version": CACHE_SCHEMA_VERSION,
            "kind": "norm",
            "scope": make_case_cache_scope(prepared.task),
            "head": plan.head,
            "q_block": plan.q_block,
            "q_start": plan.q_start,
            "q_len": plan.q_len,
            "upstream_scores": [
                {
                    "cache_key": artifact.cache_key,
                    "content_sha256": artifact.content_sha256,
                }
                for artifact in score_artifacts
            ],
        }
    )


def _writeback_cache_key(
    prepared: PreparedTask,
    plan: BlockPlan,
    norm_artifact: CachedArtifact,
) -> str:
    return sha256_json(
        {
            "cache_schema_version": CACHE_SCHEMA_VERSION,
            "kind": "writeback",
            "scope": make_case_cache_scope(prepared.task),
            "head": plan.head,
            "q_block": plan.q_block,
            "q_start": plan.q_start,
            "q_len": plan.q_len,
            "upstream_norm_key": norm_artifact.cache_key,
            "upstream_norm_sha256": norm_artifact.content_sha256,
        }
    )


def _score_artifact_meta(
    prepared: PreparedTask,
    spec: ScoreSpec,
    plan_artifact: CachedArtifact,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "scope": make_case_cache_scope(prepared.task),
        "upstream_plan": plan_artifact.content_sha256,
        "head": spec.plan.head,
        "q_block": spec.plan.q_block,
        "kv_block": spec.kv_block.index,
        "q_start": spec.plan.q_start,
        "q_len": spec.plan.q_len,
        "kv_start": spec.kv_block.start,
        "kv_len": spec.kv_block.length,
        "causal": prepared.task.causal,
        "q_block_sha256": spec.q_block_sha256,
        "k_block_sha256": spec.k_block_sha256,
        "v_block_sha256": spec.v_block_sha256,
    }


def _norm_artifact_meta(
    prepared: PreparedTask,
    plan: BlockPlan,
    score_artifacts: Iterable[CachedArtifact],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "scope": make_case_cache_scope(prepared.task),
        "head": plan.head,
        "q_block": plan.q_block,
        "q_start": plan.q_start,
        "q_len": plan.q_len,
        "upstream_scores": [
            {
                "cache_key": artifact.cache_key,
                "content_sha256": artifact.content_sha256,
            }
            for artifact in score_artifacts
        ],
    }


def _writeback_artifact_meta(
    prepared: PreparedTask,
    plan: BlockPlan,
    norm_artifact: CachedArtifact,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "scope": make_case_cache_scope(prepared.task),
        "head": plan.head,
        "q_block": plan.q_block,
        "q_start": plan.q_start,
        "q_len": plan.q_len,
        "upstream_norm_key": norm_artifact.cache_key,
        "upstream_norm_sha256": norm_artifact.content_sha256,
    }


def _arrays_from_score(block: ScoreBlock) -> dict[str, np.ndarray]:
    return {
        "scores": block.scores,
        "row_max": block.row_max,
        "row_sum": block.row_sum,
        "partial": block.partial,
    }


def _score_from_artifact(artifact: CachedArtifact) -> ScoreBlock:
    meta = artifact.meta
    return ScoreBlock(
        head=int(meta["head"]),
        q_block=int(meta["q_block"]),
        kv_block=int(meta["kv_block"]),
        q_start=int(meta["q_start"]),
        kv_start=int(meta["kv_start"]),
        scores=artifact.array("scores"),
        row_max=artifact.array("row_max"),
        row_sum=artifact.array("row_sum"),
        partial=artifact.array("partial"),
    )


def _norm_from_artifact(artifact: CachedArtifact) -> NormBlock:
    return NormBlock(
        merged_max=artifact.array("merged_max"),
        merged_denom=artifact.array("merged_denom"),
        merged_acc=artifact.array("merged_acc"),
    )


def _write_output_atomic(path: Path, output: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp.npy")
    np.save(temporary, output, allow_pickle=False)
    os.replace(temporary, path)


def _compare(
    path: str,
    actual: np.ndarray,
    expected: np.ndarray,
    *,
    atol: float,
    rtol: float,
) -> dict[str, Any] | None:
    return first_array_difference(path, actual, expected, atol=atol, rtol=rtol)


def _raise_on_difference(
    difference: dict[str, Any] | None,
    *,
    atol: float,
    rtol: float,
) -> None:
    if difference is not None:
        raise DiagnosticError(
            E_TOLERANCE_EXCEEDED,
            "tolerance exceeded during consistency audit",
            first_difference=difference,
            atol=atol,
            rtol=rtol,
        )


class FlashAttentionDiagnostics:
    def __init__(
        self,
        ledger_path: str | Path,
        store_root: str | Path,
        *,
        policy: ResourcePolicy | None = None,
        export_root: str | Path | None = None,
        initialize: bool = True,
    ) -> None:
        self.ledger = SnapshotLedger(ledger_path)
        self.store = ArtifactStore(store_root)
        self.policy = policy or ResourcePolicy()
        self.export_root = (
            Path(export_root).resolve()
            if export_root is not None
            else self.ledger.path.parent / "exports"
        )
        if initialize:
            self.ledger.initialize()

    def _load_previous_validation(
        self,
        previous: LedgerSnapshot,
    ) -> dict[str, Any]:
        validation_node = next(
            (node for node in previous.nodes if node["node_type"] == "validation"),
            None,
        )
        if validation_node is None:
            raise DiagnosticError(
                E_LEDGER_INTEGRITY,
                "previous snapshot has no validation node",
                case_id=previous.case_id,
                version=previous.version,
            )
        artifact = self.store.load(
            "validation",
            validation_node["cache_key"],
            expected_content_sha256=validation_node["content_sha256"],
        )
        return artifact.meta

    def _ensure_validation(
        self,
        prepared: PreparedTask,
        tracker: _CacheTracker,
    ) -> CachedArtifact:
        key = _validation_cache_key(prepared)
        existing = self.store.try_load("validation", key)
        if existing is not None:
            tracker.hit("validation", False)
            return existing
        tracker.miss("validation", False)
        return self.store.write(
            "validation",
            key,
            meta=prepared.validation_meta,
            arrays={
                "q": prepared.arrays.q,
                "k": prepared.arrays.k,
                "v": prepared.arrays.v,
            },
        )

    def _ensure_plan(
        self,
        prepared: PreparedTask,
        validation_artifact: CachedArtifact,
        tracker: _CacheTracker,
    ) -> CachedArtifact:
        key = _plan_cache_key(validation_artifact.cache_key, prepared)
        existing = self.store.try_load("plan", key)
        if existing is not None:
            tracker.hit("plan", False)
            return existing
        tracker.miss("plan", False)
        return self.store.write(
            "plan",
            key,
            meta=prepared.structural_plan,
        )

    def _compute_score(
        self,
        prepared: PreparedTask,
        spec: ScoreSpec,
        plan_artifact: CachedArtifact,
    ) -> CachedArtifact:
        q_all = np.asarray(prepared.arrays.q, dtype=np.float64)
        k_all = np.asarray(prepared.arrays.k, dtype=np.float64)
        v_all = np.asarray(prepared.arrays.v, dtype=np.float64)
        q_slice = q_all[
            spec.plan.head,
            spec.plan.q_start : spec.plan.q_start + spec.plan.q_len,
        ]
        kv_slice = slice(spec.kv_block.start, spec.kv_block.stop)
        block = compute_score_block(
            q_slice,
            k_all[spec.plan.head, kv_slice],
            v_all[spec.plan.head, kv_slice],
            head=spec.plan.head,
            q_block_index=spec.plan.q_block,
            kv_block_index=spec.kv_block.index,
            q_start=spec.plan.q_start,
            kv_start=spec.kv_block.start,
            causal=prepared.task.causal,
        )
        return self.store.write(
            "score",
            spec.cache_key,
            meta=_score_artifact_meta(prepared, spec, plan_artifact),
            arrays=_arrays_from_score(block),
        )

    def _ensure_norm(
        self,
        prepared: PreparedTask,
        plan: BlockPlan,
        score_artifacts: list[CachedArtifact],
        tracker: _CacheTracker,
    ) -> CachedArtifact:
        key = _norm_cache_key(prepared, plan, score_artifacts)
        existing = self.store.try_load("norm", key)
        if existing is not None:
            tracker.hit(f"norm:h{plan.head}:q{plan.q_block}", True)
            return existing
        tracker.miss(f"norm:h{plan.head}:q{plan.q_block}", True)
        merged = merge_score_blocks(_score_from_artifact(item) for item in score_artifacts)
        return self.store.write(
            "norm",
            key,
            meta=_norm_artifact_meta(prepared, plan, score_artifacts),
            arrays={
                "merged_max": merged.merged_max,
                "merged_denom": merged.merged_denom,
                "merged_acc": merged.merged_acc,
            },
        )

    def _ensure_writeback(
        self,
        prepared: PreparedTask,
        plan: BlockPlan,
        norm_artifact: CachedArtifact,
        tracker: _CacheTracker,
    ) -> CachedArtifact:
        key = _writeback_cache_key(prepared, plan, norm_artifact)
        existing = self.store.try_load("writeback", key)
        if existing is not None:
            tracker.hit(f"writeback:h{plan.head}:q{plan.q_block}", True)
            return existing
        tracker.miss(f"writeback:h{plan.head}:q{plan.q_block}", True)
        output = normalize_block(_norm_from_artifact(norm_artifact))
        return self.store.write(
            "writeback",
            key,
            meta=_writeback_artifact_meta(prepared, plan, norm_artifact),
            arrays={"output": output},
        )

    def _build_plan_records(
        self,
        prepared: PreparedTask,
        version: int,
    ) -> tuple[PlanRecord, ...]:
        records: list[PlanRecord] = []
        for plan in prepared.plans:
            kv_blocks = tuple(
                {
                    "index": block.index,
                    "start": block.start,
                    "length": block.length,
                }
                for block in plan.kv_blocks
            )
            plan_sha = sha256_json(
                {
                    "version": version,
                    "head": plan.head,
                    "q_block": plan.q_block,
                    "q_start": plan.q_start,
                    "q_len": plan.q_len,
                    "kv_blocks": list(kv_blocks),
                }
            )
            records.append(
                PlanRecord(
                    head=plan.head,
                    q_block=plan.q_block,
                    q_start=plan.q_start,
                    q_len=plan.q_len,
                    kv_blocks=kv_blocks,
                    plan_sha256=plan_sha,
                )
            )
        return tuple(records)

    def _compose_case_output(
        self,
        prepared: PreparedTask,
        writeback_by_group: dict[tuple[int, int], CachedArtifact],
        norm_by_group: dict[tuple[int, int], CachedArtifact],
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        task = prepared.task
        output = np.empty(
            (task.num_heads, task.seq_len, task.head_dim),
            dtype=np.float64,
        )
        maxima = np.empty((task.num_heads, task.seq_len), dtype=np.float64)
        denominators = np.empty((task.num_heads, task.seq_len), dtype=np.float64)
        for plan in prepared.plans:
            group = (plan.head, plan.q_block)
            writeback = writeback_by_group[group]
            norm = norm_by_group[group]
            q_slice = slice(plan.q_start, plan.q_start + plan.q_len)
            output[plan.head, q_slice] = writeback.array("output")
            maxima[plan.head, q_slice] = norm.array("merged_max")
            denominators[plan.head, q_slice] = norm.array("merged_denom")
        return output, maxima, denominators

    def _audit_output(
        self,
        prepared: PreparedTask,
        output: np.ndarray,
        maxima: np.ndarray,
        denominators: np.ndarray,
        *,
        tolerance: float,
        tolerance_rtol: float,
    ) -> dict[str, Any]:
        full_block_output, full_block_max, full_block_denom = compute_full_blockwise(
            prepared.arrays,
            prepared.plans,
            prepared.task.causal,
        )
        incremental_vs_full_difference = _compare(
            "incremental_output_vs_full_recompute",
            output,
            full_block_output,
            atol=0.0,
            rtol=0.0,
        )
        _raise_on_difference(incremental_vs_full_difference, atol=0.0, rtol=0.0)

        difference = _compare(
            "merged_max_vs_full_block_max",
            maxima,
            full_block_max,
            atol=0.0,
            rtol=0.0,
        )
        _raise_on_difference(difference, atol=0.0, rtol=0.0)
        difference = _compare(
            "merged_denominator_vs_full_block_denominator",
            denominators,
            full_block_denom,
            atol=0.0,
            rtol=0.0,
        )
        _raise_on_difference(difference, atol=0.0, rtol=0.0)

        reference_output, reference_max, reference_denom = compute_full_reference(
            prepared.arrays,
            prepared.task.causal,
        )
        difference = _compare(
            "block_output_vs_whole_sequence_output",
            output,
            reference_output,
            atol=tolerance,
            rtol=tolerance_rtol,
        )
        _raise_on_difference(
            difference,
            atol=tolerance,
            rtol=tolerance_rtol,
        )
        difference = _compare(
            "merged_max_vs_whole_sequence_max",
            maxima,
            reference_max,
            atol=tolerance,
            rtol=tolerance_rtol,
        )
        _raise_on_difference(
            difference,
            atol=tolerance,
            rtol=tolerance_rtol,
        )
        difference = _compare(
            "merged_denominator_vs_whole_sequence_denominator",
            denominators,
            reference_denom,
            atol=tolerance,
            rtol=tolerance_rtol,
        )
        _raise_on_difference(
            difference,
            atol=tolerance,
            rtol=tolerance_rtol,
        )
        return {
            "ok": True,
            "incremental_vs_full_recompute": "bitwise_equal",
            "merged_stats_vs_full_reference": "within_tolerance",
            "block_output_vs_whole_sequence": "within_tolerance",
            "atol": tolerance,
            "rtol": tolerance_rtol,
        }

    def run_manifest(
        self,
        manifest_path: str | Path,
        *,
        tolerance: float = 1e-9,
        tolerance_rtol: float = 1e-9,
        replay_check: bool = True,
    ) -> list[CaseRunResult]:
        prepared_tasks = prepare_manifest(manifest_path)
        results: list[CaseRunResult] = []
        for prepared in prepared_tasks:
            results.append(
                self.run_prepared(
                    prepared,
                    tolerance=tolerance,
                    tolerance_rtol=tolerance_rtol,
                    replay_check=replay_check,
                )
            )
        return results

    def run_prepared(
        self,
        prepared: PreparedTask,
        *,
        tolerance: float = 1e-9,
        tolerance_rtol: float = 1e-9,
        replay_check: bool = True,
    ) -> CaseRunResult:
        task = prepared.task
        previous = self.ledger.active_snapshot(
            task.case_id,
            include_children=True,
        )
        if previous is not None:
            if task.weight_revision < previous.weight_revision:
                raise DiagnosticError(
                    E_SNAPSHOT_ROLLBACK,
                    "weight revision would roll back the active snapshot",
                    case_id=task.case_id,
                    requested_weight_revision=task.weight_revision,
                    active_weight_revision=previous.weight_revision,
                )
            if task.weight_revision == previous.weight_revision:
                if prepared.arrays.qkv_sha256 != previous.input_sha256:
                    raise DiagnosticError(
                        E_INPUT_INVALID,
                        "duplicate case_id/revision has different input content",
                        case_id=task.case_id,
                        weight_revision=task.weight_revision,
                    )
                previous_task = previous.details.get("task")
                if previous_task is None:
                    previous_task = self._load_previous_validation(previous).get("task")
                if not isinstance(previous_task, dict) or not self._same_case_configuration(
                    task,
                    previous_task,
                ):
                    raise DiagnosticError(
                        E_INPUT_INVALID,
                        "duplicate case_id/revision has a different case configuration",
                        case_id=task.case_id,
                        weight_revision=task.weight_revision,
                    )
                verification = self.verify_case(
                    task.case_id,
                    version=previous.version,
                    tolerance=tolerance,
                    tolerance_rtol=tolerance_rtol,
                )
                return CaseRunResult(
                    case_id=task.case_id,
                    business_code="OK",
                    snapshot_version=previous.version,
                    weight_revision=previous.weight_revision,
                    idempotent_reuse=True,
                    input_sha256=previous.input_sha256,
                    output_sha256=previous.output_sha256,
                    output_path=previous.output_path,
                    recomputed=[],
                    reused=[],
                    changed_heads=[],
                    affected_q_blocks=[],
                    resource=ResourceUsage(),
                    verification=verification.to_dict(),
                    cache_hits=0,
                    cache_misses=0,
                )
            previous_task = previous.details.get("task")
            if previous_task is None:
                previous_task = self._load_previous_validation(previous).get("task")
            if not isinstance(previous_task, dict) or not self._same_case_configuration(
                task,
                previous_task,
            ):
                raise DiagnosticError(
                    E_INPUT_INVALID,
                    "weight revision changes immutable case configuration",
                    case_id=task.case_id,
                )

        version = 1 if previous is None else previous.version + 1
        previous_validation = (
            self._load_previous_validation(previous) if previous is not None else None
        )
        if previous_validation is None:
            changed = list(range(task.num_heads))
            affected = [f"h{plan.head}:q{plan.q_block}" for plan in prepared.plans]
        else:
            changed = changed_heads(previous_validation, prepared)
            affected = affected_q_blocks_from_hashes(previous_validation, prepared)

        tracker = _CacheTracker()
        validation_artifact = self._ensure_validation(prepared, tracker)
        plan_artifact = self._ensure_plan(prepared, validation_artifact, tracker)
        score_specs = _score_specs(prepared, plan_artifact)

        groups: dict[tuple[int, int], list[ScoreSpec]] = {}
        for spec in score_specs:
            groups.setdefault((spec.plan.head, spec.plan.q_block), []).append(spec)

        score_artifacts: dict[tuple[int, int, int], CachedArtifact] = {}
        missing_groups: dict[tuple[int, int], list[ScoreSpec]] = {}
        for group_key, specs in groups.items():
            missing: list[ScoreSpec] = []
            for spec in sorted(specs, key=lambda item: item.kv_block.index):
                existing = self.store.try_load("score", spec.cache_key)
                if existing is None:
                    missing.append(spec)
                else:
                    tracker.hit(spec.node_id, True)
                    score_artifacts[(spec.plan.head, spec.plan.q_block, spec.kv_block.index)] = (
                        existing
                    )
            if missing:
                missing_groups[group_key] = missing

        plan_by_group = {
            (plan.head, plan.q_block): plan
            for plan in prepared.plans
        }
        per_worker_bytes = max(
            (
                estimate_q_block_resident_bytes(task, plan_by_group[group])
                for group in missing_groups
            ),
            default=0,
        )
        resource = select_parallel_workers(
            len(missing_groups),
            per_worker_bytes,
            self.policy,
        )

        def compute_group(group_key: tuple[int, int]) -> list[CachedArtifact]:
            computed: list[CachedArtifact] = []
            for spec in sorted(
                missing_groups[group_key],
                key=lambda item: item.kv_block.index,
            ):
                artifact = self._compute_score(prepared, spec, plan_artifact)
                computed.append(artifact)
            return computed

        if missing_groups:
            if resource.parallel_workers_used > 1:
                with ThreadPoolExecutor(
                    max_workers=resource.parallel_workers_used,
                    thread_name_prefix="flashdiag-qblock",
                ) as executor:
                    for group_key, artifacts in zip(
                        sorted(missing_groups),
                        executor.map(compute_group, sorted(missing_groups)),
                        strict=True,
                    ):
                        specs = sorted(
                            missing_groups[group_key],
                            key=lambda item: item.kv_block.index,
                        )
                        for spec, artifact in zip(specs, artifacts, strict=True):
                            tracker.miss(spec.node_id, True)
                            score_artifacts[
                                (
                                    spec.plan.head,
                                    spec.plan.q_block,
                                    spec.kv_block.index,
                                )
                            ] = artifact
            else:
                for group_key in sorted(missing_groups):
                    artifacts = compute_group(group_key)
                    specs = sorted(
                        missing_groups[group_key],
                        key=lambda item: item.kv_block.index,
                    )
                    for spec, artifact in zip(specs, artifacts, strict=True):
                        tracker.miss(spec.node_id, True)
                        score_artifacts[
                            (
                                spec.plan.head,
                                spec.plan.q_block,
                                spec.kv_block.index,
                            )
                        ] = artifact

        norm_by_group: dict[tuple[int, int], CachedArtifact] = {}
        writeback_by_group: dict[tuple[int, int], CachedArtifact] = {}
        for plan in prepared.plans:
            group_key = (plan.head, plan.q_block)
            ordered_scores = [
                score_artifacts[(plan.head, plan.q_block, kv_block.index)]
                for kv_block in plan.kv_blocks
            ]
            norm_artifact = self._ensure_norm(
                prepared,
                plan,
                ordered_scores,
                tracker,
            )
            writeback_artifact = self._ensure_writeback(
                prepared,
                plan,
                norm_artifact,
                tracker,
            )
            norm_by_group[group_key] = norm_artifact
            writeback_by_group[group_key] = writeback_artifact

        output, maxima, denominators = self._compose_case_output(
            prepared,
            writeback_by_group,
            norm_by_group,
        )
        audit = self._audit_output(
            prepared,
            output,
            maxima,
            denominators,
            tolerance=tolerance,
            tolerance_rtol=tolerance_rtol,
        )
        output_sha = array_sha256(output)
        output_path = (
            self.export_root / task.case_id / f"v{version}" / "output.npy"
        )
        _write_output_atomic(output_path, output)

        recomputed, reused = tracker.finish()
        plan_records = self._build_plan_records(prepared, version)
        node_records = self._node_records(
            prepared,
            version,
            validation_artifact,
            plan_artifact,
            score_specs,
            score_artifacts,
            norm_by_group,
            writeback_by_group,
        )
        details = {
            "business_code": "OK",
            "snapshot_version": version,
            "weight_revision": task.weight_revision,
            "task": task.canonical(),
            "changed_heads": changed,
            "affected_q_blocks": affected,
            "recomputed": recomputed,
            "reused": reused,
            "resource": resource.to_dict(),
            "verification": audit,
            "cache_hits": tracker.hits,
            "cache_misses": tracker.misses,
        }
        record = SnapshotRecord(
            case_id=task.case_id,
            version=version,
            weight_revision=task.weight_revision,
            input_sha256=prepared.arrays.qkv_sha256,
            output_sha256=output_sha,
            output_path=str(output_path),
            previous_snapshot_id=previous.snapshot_id if previous is not None else None,
            nodes=tuple(node_records),
            plans=plan_records,
            details=details,
        )
        committed = self.ledger.commit(
            record,
            expected_active_snapshot_id=(
                previous.snapshot_id if previous is not None else None
            ),
        )
        verification_report = audit
        if replay_check:
            replay_result = self.verify_case(
                task.case_id,
                version=committed.version,
                tolerance=tolerance,
                tolerance_rtol=tolerance_rtol,
            )
            verification_report = {
                **audit,
                "ledger_replay": replay_result.to_dict(),
            }

        return CaseRunResult(
            case_id=task.case_id,
            business_code="OK",
            snapshot_version=committed.version,
            weight_revision=committed.weight_revision,
            idempotent_reuse=False,
            input_sha256=committed.input_sha256,
            output_sha256=committed.output_sha256,
            output_path=committed.output_path,
            recomputed=recomputed,
            reused=reused,
            changed_heads=changed,
            affected_q_blocks=affected,
            resource=resource,
            verification=verification_report,
            cache_hits=tracker.hits,
            cache_misses=tracker.misses,
        )

    @staticmethod
    def _same_case_configuration(task: TaskSpec, previous_task: dict[str, Any]) -> bool:
        immutable = (
            "case_id",
            "seq_len",
            "head_dim",
            "num_heads",
            "q_block_len",
            "kv_block_len",
            "dtype",
            "causal",
            "seed",
        )
        current = task.canonical()
        return all(previous_task.get(key) == current.get(key) for key in immutable)

    def _node_records(
        self,
        prepared: PreparedTask,
        version: int,
        validation_artifact: CachedArtifact,
        plan_artifact: CachedArtifact,
        score_specs: list[ScoreSpec],
        score_artifacts: dict[tuple[int, int, int], CachedArtifact],
        norm_by_group: dict[tuple[int, int], CachedArtifact],
        writeback_by_group: dict[tuple[int, int], CachedArtifact],
    ) -> list[NodeRecord]:
        records: list[NodeRecord] = [
            NodeRecord(
                node_id="validation",
                node_type="validation",
                version=version,
                cache_key=validation_artifact.cache_key,
                content_sha256=validation_artifact.content_sha256,
                artifact_path=validation_artifact.path,
                dep_hashes={
                    "q": prepared.arrays.q_sha256,
                    "k": prepared.arrays.k_sha256,
                    "v": prepared.arrays.v_sha256,
                    "qkv": prepared.arrays.qkv_sha256,
                },
            ),
            NodeRecord(
                node_id="plan",
                node_type="plan",
                version=version,
                cache_key=plan_artifact.cache_key,
                content_sha256=plan_artifact.content_sha256,
                artifact_path=plan_artifact.path,
                dep_hashes={
                    "validation": validation_artifact.content_sha256,
                    "structural_sha256": prepared.structural_sha256,
                },
            ),
        ]
        for spec in sorted(score_specs, key=lambda item: item.node_id):
            artifact = score_artifacts[
                (spec.plan.head, spec.plan.q_block, spec.kv_block.index)
            ]
            records.append(
                NodeRecord(
                    node_id=spec.node_id,
                    node_type="score",
                    version=version,
                    cache_key=artifact.cache_key,
                    content_sha256=artifact.content_sha256,
                    artifact_path=artifact.path,
                    dep_hashes={
                        "plan": plan_artifact.content_sha256,
                        "q_block": spec.q_block_sha256,
                        "k_block": spec.k_block_sha256,
                        "v_block": spec.v_block_sha256,
                    },
                )
            )
        for plan in sorted(prepared.plans, key=lambda item: (item.head, item.q_block)):
            group = (plan.head, plan.q_block)
            norm = norm_by_group[group]
            writeback = writeback_by_group[group]
            records.append(
                NodeRecord(
                    node_id=f"norm:h{plan.head}:q{plan.q_block}",
                    node_type="norm",
                    version=version,
                    cache_key=norm.cache_key,
                    content_sha256=norm.content_sha256,
                    artifact_path=norm.path,
                    dep_hashes={
                        "scores": {
                            str(kv_block.index): score_artifacts[
                                (plan.head, plan.q_block, kv_block.index)
                            ].content_sha256
                            for kv_block in plan.kv_blocks
                        }
                    },
                )
            )
            records.append(
                NodeRecord(
                    node_id=f"writeback:h{plan.head}:q{plan.q_block}",
                    node_type="writeback",
                    version=version,
                    cache_key=writeback.cache_key,
                    content_sha256=writeback.content_sha256,
                    artifact_path=writeback.path,
                    dep_hashes={"norm": norm.content_sha256},
                )
            )
        return records

    def verify_case(
        self,
        case_id: str,
        *,
        version: int | None = None,
        tolerance: float = 1e-9,
        tolerance_rtol: float = 1e-9,
    ) -> VerificationResult:
        snapshot = self.ledger.replay(case_id, version=version)
        for node in snapshot.nodes:
            if node["version"] != snapshot.version:
                raise DiagnosticError(
                    E_LEDGER_INTEGRITY,
                    "node version differs from snapshot version",
                    case_id=case_id,
                    version=snapshot.version,
                    node_id=node["node_id"],
                )
            if node["dependency_sha256"] != sha256_json(node["dep_hashes"]):
                raise DiagnosticError(
                    E_LEDGER_INTEGRITY,
                    "node dependency checksum does not replay",
                    case_id=case_id,
                    version=snapshot.version,
                    node_id=node["node_id"],
                )
            expected_artifact_path = self.store.manifest_path(
                node["node_type"],
                node["cache_key"],
            ).resolve()
            if Path(node["artifact_path"]).resolve() != expected_artifact_path:
                raise DiagnosticError(
                    E_LEDGER_INTEGRITY,
                    "node artifact path does not match its content-addressed key",
                    case_id=case_id,
                    version=snapshot.version,
                    node_id=node["node_id"],
                )
        if any(plan["version"] != snapshot.version for plan in snapshot.plans):
            raise DiagnosticError(
                E_LEDGER_INTEGRITY,
                "block plan version differs from snapshot version",
                case_id=case_id,
                version=snapshot.version,
            )
        node_map = {
            node["node_id"]: node
            for node in snapshot.nodes
        }
        validation_node = node_map.get("validation")
        plan_node = node_map.get("plan")
        if validation_node is None or plan_node is None:
            raise DiagnosticError(
                E_LEDGER_INTEGRITY,
                "snapshot is missing validation or plan nodes",
                case_id=case_id,
                version=snapshot.version,
            )
        validation_artifact = self.store.load(
            "validation",
            validation_node["cache_key"],
            expected_content_sha256=validation_node["content_sha256"],
        )
        task_record = validation_artifact.meta["task"]
        task = TaskSpec.from_record(task_record, Path("/"))
        committed_arrays = array_set_from_qkv(
            task,
            validation_artifact.array("q"),
            validation_artifact.array("k"),
            validation_artifact.array("v"),
        )
        prepared = prepare_arrays(task, committed_arrays)
        plan_keys = {(plan["head"], plan["q_block"]) for plan in snapshot.plans}
        expected_plan_keys = {(plan.head, plan.q_block) for plan in prepared.plans}
        if plan_keys != expected_plan_keys:
            raise DiagnosticError(
                E_LEDGER_INTEGRITY,
                "block plan set does not match the replayed structural plan",
                case_id=case_id,
                version=snapshot.version,
            )
        if prepared.arrays.qkv_sha256 != snapshot.input_sha256:
            raise DiagnosticError(
                E_LEDGER_INTEGRITY,
                "input arrays no longer match the committed snapshot",
                case_id=case_id,
                version=snapshot.version,
                expected=snapshot.input_sha256,
                actual=prepared.arrays.qkv_sha256,
            )
        expected_validation_key = _validation_cache_key(prepared)
        if expected_validation_key != validation_node["cache_key"]:
            raise DiagnosticError(
                E_LEDGER_INTEGRITY,
                "validation cache key does not replay",
                case_id=case_id,
                expected=expected_validation_key,
                actual=validation_node["cache_key"],
            )
        plan_artifact = self.store.load(
            "plan",
            plan_node["cache_key"],
            expected_content_sha256=plan_node["content_sha256"],
        )
        expected_plan_key = _plan_cache_key(validation_artifact.cache_key, prepared)
        if expected_plan_key != plan_node["cache_key"]:
            raise DiagnosticError(
                E_LEDGER_INTEGRITY,
                "plan cache key does not replay",
                case_id=case_id,
                expected=expected_plan_key,
                actual=plan_node["cache_key"],
            )

        score_specs = _score_specs(prepared, plan_artifact)
        expected_node_ids = {"validation", "plan", *[spec.node_id for spec in score_specs]}
        for plan in prepared.plans:
            expected_node_ids.add(f"norm:h{plan.head}:q{plan.q_block}")
            expected_node_ids.add(f"writeback:h{plan.head}:q{plan.q_block}")
        if set(node_map) != expected_node_ids:
            raise DiagnosticError(
                E_LEDGER_INTEGRITY,
                "ledger node set does not match the expected pipeline graph",
                case_id=case_id,
                version=snapshot.version,
                missing=sorted(expected_node_ids - set(node_map)),
                extra=sorted(set(node_map) - expected_node_ids),
            )
        score_artifacts: dict[tuple[int, int, int], CachedArtifact] = {}
        for spec in score_specs:
            node = node_map.get(spec.node_id)
            if node is None:
                raise DiagnosticError(
                    E_LEDGER_INTEGRITY,
                    "score node is missing from ledger",
                    case_id=case_id,
                    node_id=spec.node_id,
                )
            if node["cache_key"] != spec.cache_key:
                raise DiagnosticError(
                    E_LEDGER_INTEGRITY,
                    "score cache key does not replay",
                    case_id=case_id,
                    node_id=spec.node_id,
                    expected=spec.cache_key,
                    actual=node["cache_key"],
                )
            artifact = self.store.load(
                "score",
                node["cache_key"],
                expected_content_sha256=node["content_sha256"],
            )
            score_artifacts[(spec.plan.head, spec.plan.q_block, spec.kv_block.index)] = (
                artifact
            )

        norm_by_group: dict[tuple[int, int], CachedArtifact] = {}
        writeback_by_group: dict[tuple[int, int], CachedArtifact] = {}
        checks: list[str] = [
            "snapshot_checksum_replay",
            "validation_cache_key_replay",
            "plan_cache_key_replay",
            "score_cache_key_replay",
            "artifact_checksums",
        ]
        for plan in prepared.plans:
            ordered_scores = [
                score_artifacts[(plan.head, plan.q_block, block.index)]
                for block in plan.kv_blocks
            ]
            expected_norm_key = _norm_cache_key(prepared, plan, ordered_scores)
            norm_node = node_map.get(f"norm:h{plan.head}:q{plan.q_block}")
            if norm_node is None or norm_node["cache_key"] != expected_norm_key:
                raise DiagnosticError(
                    E_LEDGER_INTEGRITY,
                    "norm cache key does not replay",
                    case_id=case_id,
                    head=plan.head,
                    q_block=plan.q_block,
                )
            norm_artifact = self.store.load(
                "norm",
                norm_node["cache_key"],
                expected_content_sha256=norm_node["content_sha256"],
            )
            recomputed_norm = merge_score_blocks(
                _score_from_artifact(item) for item in ordered_scores
            )
            stored_norm = _norm_from_artifact(norm_artifact)
            difference = _compare(
                f"replayed_norm:h{plan.head}:q{plan.q_block}:max",
                recomputed_norm.merged_max,
                stored_norm.merged_max,
                atol=tolerance,
                rtol=tolerance_rtol,
            )
            _raise_on_difference(
                difference,
                atol=tolerance,
                rtol=tolerance_rtol,
            )
            difference = _compare(
                f"replayed_norm:h{plan.head}:q{plan.q_block}:denominator",
                recomputed_norm.merged_denom,
                stored_norm.merged_denom,
                atol=tolerance,
                rtol=tolerance_rtol,
            )
            _raise_on_difference(
                difference,
                atol=tolerance,
                rtol=tolerance_rtol,
            )
            difference = _compare(
                f"replayed_norm:h{plan.head}:q{plan.q_block}:accumulator",
                recomputed_norm.merged_acc,
                stored_norm.merged_acc,
                atol=tolerance,
                rtol=tolerance_rtol,
            )
            _raise_on_difference(
                difference,
                atol=tolerance,
                rtol=tolerance_rtol,
            )

            expected_writeback_key = _writeback_cache_key(
                prepared,
                plan,
                norm_artifact,
            )
            writeback_node = node_map.get(f"writeback:h{plan.head}:q{plan.q_block}")
            if (
                writeback_node is None
                or writeback_node["cache_key"] != expected_writeback_key
            ):
                raise DiagnosticError(
                    E_LEDGER_INTEGRITY,
                    "writeback cache key does not replay",
                    case_id=case_id,
                    head=plan.head,
                    q_block=plan.q_block,
                )
            writeback_artifact = self.store.load(
                "writeback",
                writeback_node["cache_key"],
                expected_content_sha256=writeback_node["content_sha256"],
            )
            replayed_output = normalize_block(recomputed_norm)
            difference = _compare(
                f"replayed_output:h{plan.head}:q{plan.q_block}",
                replayed_output,
                writeback_artifact.array("output"),
                atol=tolerance,
                rtol=tolerance_rtol,
            )
            _raise_on_difference(
                difference,
                atol=tolerance,
                rtol=tolerance_rtol,
            )
            norm_by_group[(plan.head, plan.q_block)] = norm_artifact
            writeback_by_group[(plan.head, plan.q_block)] = writeback_artifact

        output, maxima, denominators = self._compose_case_output(
            prepared,
            writeback_by_group,
            norm_by_group,
        )
        output_sha = array_sha256(output)
        if output_sha != snapshot.output_sha256:
            raise DiagnosticError(
                E_LEDGER_INTEGRITY,
                "replayed output checksum differs from snapshot",
                case_id=case_id,
                version=snapshot.version,
                expected=snapshot.output_sha256,
                actual=output_sha,
            )
        output_file = Path(snapshot.output_path)
        if not output_file.is_file():
            raise DiagnosticError(
                E_LEDGER_INTEGRITY,
                "committed output export file is missing",
                case_id=case_id,
                version=snapshot.version,
                path=str(output_file),
            )
        exported = np.load(output_file, allow_pickle=False)
        if array_sha256(exported) != snapshot.output_sha256:
            raise DiagnosticError(
                E_LEDGER_INTEGRITY,
                "committed output export checksum differs from snapshot",
                case_id=case_id,
                version=snapshot.version,
            )
        reference_output, reference_max, reference_denom = compute_full_reference(
            prepared.arrays,
            prepared.task.causal,
        )
        full_block_output, full_block_max, full_block_denom = compute_full_blockwise(
            prepared.arrays,
            prepared.plans,
            prepared.task.causal,
        )
        assert_arrays_equal(
            "ledger_replay_vs_full_block_recompute",
            output,
            full_block_output,
            atol=0.0,
            rtol=0.0,
        )
        assert_arrays_equal(
            "ledger_max_vs_full_block_max",
            maxima,
            full_block_max,
            atol=0.0,
            rtol=0.0,
        )
        assert_arrays_equal(
            "ledger_denom_vs_full_block_denom",
            denominators,
            full_block_denom,
            atol=0.0,
            rtol=0.0,
        )
        assert_arrays_equal(
            "ledger_replay_vs_whole_sequence",
            output,
            reference_output,
            atol=tolerance,
            rtol=tolerance_rtol,
        )
        assert_arrays_equal(
            "ledger_max_vs_whole_sequence",
            maxima,
            reference_max,
            atol=tolerance,
            rtol=tolerance_rtol,
        )
        assert_arrays_equal(
            "ledger_denom_vs_whole_sequence",
            denominators,
            reference_denom,
            atol=tolerance,
            rtol=tolerance_rtol,
        )
        checks.extend(
            [
                "norm_partial_replay",
                "writeback_replay",
                "output_checksum_replay",
                "incremental_vs_full_block_recompute",
                "merged_stats_vs_whole_sequence",
            ]
        )
        return VerificationResult(
            case_id=case_id,
            snapshot_version=snapshot.version,
            ok=True,
            tolerance=tolerance,
            tolerance_rtol=tolerance_rtol,
            output_sha256=output_sha,
            node_count=len(snapshot.nodes),
            checks=checks,
            first_difference=None,
        )

    def query_case(
        self,
        case_id: str,
        *,
        version: int | None = None,
        include_children: bool = True,
    ) -> dict[str, Any]:
        snapshot = self.ledger.get_snapshot(
            case_id,
            version=version,
            include_children=include_children,
        )
        return snapshot.to_dict(include_children=include_children)

    def history(self, case_id: str) -> list[dict[str, Any]]:
        return self.ledger.history(case_id)

    def store_index(self) -> dict[str, Any]:
        return self.store.index()

