"""End-to-end deterministic pipeline with resumable stage checkpoints."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .canonical import (
    read_json,
    sha256_file,
    sha256_value,
    stable_float,
    write_json_atomic,
    write_jsonl_atomic,
)
from .consistency import verify_consistency
from .errors import (
    E_CONFIG,
    E_INCONSISTENT,
    E_WORKER,
    RecallError,
    error_dict,
)
from .executor import (
    LocalWorkerPool,
    build_projection_tasks,
    build_statistics_tasks,
    merge_worker_envelopes,
)
from .ingest import IngestResult, ingest_dataset
from .inverted import merge_chunks, merge_global_partitions
from .models import Document, Query
from .projection import merge_document_chunks
from .sample_data import GENERATOR_VERSION
from .scheduler import (
    SchedulerConfig,
    accepted_decisions,
    build_plan,
    execution_ids,
)
from .scoring import COMPARE_MODES, score_query
from .snapshots import (
    VersionedSnapshotStore,
    checkpoint_file,
    statistics_snapshot_file,
)
from .weights import WeightTable, load_weight_table


STATE_SCHEMA = "sparse-recall.state.v1"


@dataclass
class PipelineConfig:
    workers: int = 2
    use_process_pool: bool = True
    chunk_size: int = 8
    compensated: bool = True
    decimals: int = 12
    subnormal_floor: float = 1.0e-300
    threshold: float | None = 0.0
    compare_mode: str = "strict"
    tolerance: float = 1.0e-9
    crash_after_commits: int | None = None
    injected_failures: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)

    def validate(self) -> None:
        if self.workers < 1:
            raise RecallError(E_CONFIG, "pipeline", "workers must be >= 1")
        if self.chunk_size < 1:
            raise RecallError(E_CONFIG, "pipeline", "chunk_size must be >= 1")
        if self.decimals < 3 or self.decimals > 15:
            raise RecallError(E_CONFIG, "pipeline", "decimals must be in [3, 15]")
        if self.subnormal_floor < 0:
            raise RecallError(E_CONFIG, "pipeline", "subnormal_floor must be >= 0")
        if self.compare_mode not in COMPARE_MODES:
            raise RecallError(
                E_CONFIG,
                "pipeline",
                f"compare_mode must be one of {sorted(COMPARE_MODES)}",
            )
        if self.tolerance < 0:
            raise RecallError(E_CONFIG, "pipeline", "tolerance must be >= 0")
        if self.crash_after_commits is not None and self.crash_after_commits < 1:
            raise RecallError(
                E_CONFIG,
                "pipeline",
                "crash_after_commits must be >= 1 when set",
            )

    def plan_config(self, ingest: IngestResult, weights: WeightTable) -> SchedulerConfig:
        avg_doc_nnz = 1
        if ingest.documents:
            average_terms = sum(len(doc.terms) for doc in ingest.documents) / len(
                ingest.documents
            )
            avg_doc_nnz = max(1, int(round(average_terms * weights.nnz_per_term)))
        return SchedulerConfig(
            slots=max(1, self.workers),
            fma_budget=0,
            memory_budget=0,
            worker_shards=max(1, self.workers),
            chunk_size=self.chunk_size,
            query_candidate_docs=len(ingest.documents),
            avg_doc_nnz=avg_doc_nnz,
        )

    def semantic_dict(self) -> dict[str, Any]:
        return {
            "chunk_size": self.chunk_size,
            "compensated": self.compensated,
            "decimals": self.decimals,
            "subnormal_floor": stable_float(self.subnormal_floor, self.decimals),
            "threshold": None
            if self.threshold is None
            else stable_float(self.threshold, self.decimals),
            "compare_mode": self.compare_mode,
            "tolerance": stable_float(self.tolerance, self.decimals),
        }


@dataclass
class PipelineResult:
    workdir: Path
    output_dir: Path
    report: dict[str, Any]


class _RunState:
    def __init__(self) -> None:
        self.payload: dict[str, Any] = {
            "schema": STATE_SCHEMA,
            "documents": {},
            "queries": {},
            "partitions": {},
            "batch_postings": {},
            "global_stats": merge_global_partitions([], compensated=True, decimals=12),
            "vectors": {},
            "query_vectors": {},
            "results": {},
        }
        self.completed: dict[str, set[str]] = {
            stage: set() for stage in ("ingest", "inverted", "projection", "scoring")
        }
        self.failed: dict[str, dict[str, Any]] = {
            stage: {} for stage in ("ingest", "inverted", "projection", "scoring")
        }
        self.reused: dict[str, set[str]] = {
            stage: set() for stage in ("ingest", "inverted", "projection", "scoring")
        }
        self.recomputed: dict[str, set[str]] = {
            stage: set() for stage in ("ingest", "inverted", "projection", "scoring")
        }

    @classmethod
    def from_snapshot(cls, snapshot: dict[str, Any]) -> "_RunState":
        state = cls()
        payload = snapshot.get("payload")
        if not isinstance(payload, dict) or payload.get("schema") != STATE_SCHEMA:
            raise RecallError(
                E_INCONSISTENT,
                "load",
                "checkpoint payload has an unsupported schema",
            )
        state.payload = payload
        if state.payload.get("global_stats") is None:
            state.payload["global_stats"] = merge_global_partitions(
                [],
                compensated=True,
                decimals=12,
            )
        for stage in state.completed:
            state.completed[stage] = set(
                snapshot.get("completed_batches", {}).get(stage, [])
            )
            state.failed[stage] = {
                key: value
                for key, value in snapshot.get("failed_batches", {})
                .get(stage, {})
                .items()
            }
        return state

    def completed_dict(self) -> dict[str, list[str]]:
        return {stage: sorted(values) for stage, values in sorted(self.completed.items())}

    def failed_dict(self) -> dict[str, dict[str, Any]]:
        return {
            stage: {key: values[key] for key in sorted(values)}
            for stage, values in sorted(self.failed.items())
        }

    def has_unit(self, stage: str, key: str) -> bool:
        return key in self.completed[stage] or key in self.failed[stage]

    def mark_completed(self, stage: str, key: str) -> None:
        self.completed[stage].add(key)
        self.failed[stage].pop(key, None)
        self.recomputed[stage].add(key)

    def mark_failed(self, stage: str, key: str, error: dict[str, Any]) -> None:
        self.failed[stage][key] = error
        self.completed[stage].discard(key)
        self.recomputed[stage].add(key)

    def mark_reused(self, stage: str, key: str) -> None:
        if self.has_unit(stage, key):
            self.reused[stage].add(key)


def _default_state() -> dict[str, Any]:
    return _RunState().payload


def _input_digest(
    document_path: str | Path,
    query_path: str | Path,
    weights: WeightTable,
) -> str:
    return sha256_value(
        {
            "documents_sha256": sha256_file(document_path),
            "queries_sha256": sha256_file(query_path),
            "weights_content_sha256": weights.content_hash,
        }
    )


def _run_signature(
    plan_sha256: str,
    weights: WeightTable,
    config: PipelineConfig,
) -> str:
    return sha256_value(
        {
            "plan_sha256": plan_sha256,
            "weights_content_sha256": weights.content_hash,
            "config": config.semantic_dict(),
        }
    )


def _merge_batch_postings(
    target: dict[str, list[dict[str, Any]]],
    incoming: dict[str, list[dict[str, Any]]],
) -> None:
    for term, entries in incoming.items():
        target.setdefault(term, []).extend(entries)
        target[term].sort(key=lambda item: item["doc_id"])


def _global_postings(payload: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    merged: dict[str, list[dict[str, Any]]] = {}
    for batch_key in sorted(payload["batch_postings"], key=lambda item: int(item)):
        _merge_batch_postings(merged, payload["batch_postings"][batch_key])
    return {term: merged[term] for term in sorted(merged)}


def _write_plan(path: Path, plan: dict[str, Any]) -> str:
    digest = sha256_value(plan)
    write_json_atomic(path, plan)
    return digest


def _query_decision_queries(
    ingest: IngestResult,
    decision: dict[str, Any],
) -> list[Query]:
    batch_no = int(decision["batch_no"])
    count = int(decision["execution"]["count"])
    return ingest.query_batches[batch_no][:count]


def _doc_decision_documents(
    ingest: IngestResult,
    decision: dict[str, Any],
) -> list[Document]:
    batch_no = int(decision["batch_no"])
    count = int(decision["execution"]["count"])
    return ingest.doc_batches[batch_no][:count]


class RecallPipeline:
    def __init__(
        self,
        *,
        document_path: str | Path,
        query_path: str | Path,
        weights_path: str | Path,
        workdir: str | Path,
        scheduler_config: SchedulerConfig,
        config: PipelineConfig,
    ) -> None:
        self.document_path = Path(document_path)
        self.query_path = Path(query_path)
        self.weights_path = Path(weights_path)
        self.workdir = Path(workdir)
        self.output_dir = self.workdir / "outputs"
        self.scheduler_config = scheduler_config
        self.config = config
        self.config.validate()
        self.weights = load_weight_table(str(self.weights_path))
        self.ingest = ingest_dataset(self.document_path, self.query_path, self.weights)
        self.plan = build_plan(self.ingest, self.weights, scheduler_config)
        self.plan_path = self.workdir / "plan.json"
        self.plan_sha256 = sha256_value(self.plan)
        self.inputs_sha256 = _input_digest(
            self.document_path,
            self.query_path,
            self.weights,
        )
        self.run_signature = _run_signature(
            self.plan_sha256,
            self.weights,
            self.config,
        )
        self.checkpoint_store = VersionedSnapshotStore(
            checkpoint_file(self.workdir),
            snapshot_kind="pipeline-checkpoint",
        )
        self.statistics_store = VersionedSnapshotStore(
            statistics_snapshot_file(self.workdir),
            snapshot_kind="statistics",
        )
        self.state: _RunState | None = None
        self.version = 0
        self.commits_this_run = 0
        self.initial_version = 0
        self.resumed = False

    @property
    def metadata(self) -> dict[str, Any]:
        return {
            "plan_sha256": self.plan_sha256,
            "inputs_sha256": self.inputs_sha256,
            "weights_content_sha256": self.weights.content_hash,
            "run_signature": self.run_signature,
            "generator_schema": GENERATOR_VERSION,
        }

    def _check_existing_plan(self) -> None:
        if not self.plan_path.exists():
            _write_plan(self.plan_path, self.plan)
            return
        try:
            existing = read_json(self.plan_path)
        except Exception:
            _write_plan(self.plan_path, self.plan)
            return
        if sha256_value(existing) != self.plan_sha256:
            if self.checkpoint_store.load() is None:
                _write_plan(self.plan_path, self.plan)
            else:
                raise RecallError(
                    E_INCONSISTENT,
                    "load",
                    "existing plan differs from the deterministic recomputation",
                )
        else:
            write_json_atomic(self.plan_path, self.plan)

    def _load_or_initialize(self) -> None:
        snapshot = self.checkpoint_store.load()
        if snapshot is None:
            self.state = _RunState()
            self.state.payload["global_stats"] = merge_global_partitions(
                [],
                compensated=self.config.compensated,
                decimals=self.config.decimals,
            )
            self.version = 0
            self.initial_version = 0
            self.resumed = False
            for stage in self.state.completed:
                self.state.recomputed[stage].clear()
            return
        metadata = snapshot.get("metadata", {})
        if metadata.get("run_signature") != self.run_signature:
            raise RecallError(
                E_INCONSISTENT,
                "load",
                "checkpoint was created with different inputs or configuration",
                details={
                    "checkpoint_run_signature": metadata.get("run_signature"),
                    "current_run_signature": self.run_signature,
                },
            )
        self.state = _RunState.from_snapshot(snapshot)
        if not self.state.payload["partitions"]:
            self.state.payload["global_stats"] = merge_global_partitions(
                [],
                compensated=self.config.compensated,
                decimals=self.config.decimals,
            )
        self.version = int(snapshot["version"])
        self.initial_version = self.version
        self.resumed = True
        for stage in self.state.completed:
            self.state.reused[stage].update(self.state.completed[stage])
            self.state.reused[stage].update(self.state.failed[stage])

    def _commit(self, stage: str) -> None:
        assert self.state is not None
        version = self.version + 1
        snapshot = self.checkpoint_store.commit(
            version=version,
            stage=stage,
            payload=self.state.payload,
            completed_batches=self.state.completed_dict(),
            failed_batches=self.state.failed_dict(),
            metadata=self.metadata,
        )
        self.version = int(snapshot["version"])
        self.commits_this_run += 1
        if (
            self.config.crash_after_commits is not None
            and self.commits_this_run >= self.config.crash_after_commits
        ):
            sys.stdout.flush()
            sys.stderr.flush()
            os._exit(137)

    def _sync_statistics_snapshot(self) -> None:
        assert self.state is not None
        if self.state.payload.get("global_stats") is None:
            return
        if self.statistics_store.current_version() >= self.version:
            return
        self.statistics_store.commit(
            version=self.version,
            stage="inverted",
            payload={
                "global_stats": self.state.payload["global_stats"],
                "partitions": self.state.payload["partitions"],
                "reduction_order": "batch_no,chunk_index,doc_id,term",
                "compensated": self.config.compensated,
            },
            completed_batches=self.state.completed_dict(),
            failed_batches=self.state.failed_dict(),
            metadata=self.metadata,
        )

    def _ingest_stage(self) -> None:
        assert self.state is not None
        for decision in accepted_decisions(self.plan):
            key = decision["batch_key"]
            if self.state.has_unit("ingest", key):
                self.state.mark_reused("ingest", key)
                continue
            if decision["kind"] == "doc":
                docs = self.ingest.doc_batches[int(decision["batch_no"])]
                for document in docs:
                    self.state.payload["documents"][document.doc_id] = document.to_dict()
            else:
                queries = self.ingest.query_batches[int(decision["batch_no"])]
                for query in queries:
                    self.state.payload["queries"][query.query_id] = query.to_dict()
            self.state.mark_completed("ingest", key)
            self._commit("ingest")

    def _decision_waves(self) -> list[list[dict[str, Any]]]:
        decisions = {
            item["batch_key"]: item for item in accepted_decisions(self.plan)
        }
        return [
            [decisions[key] for key in wave["batches"] if key in decisions]
            for wave in self.plan["waves"]
        ]

    def _injected_failure(
        self,
        key: str,
        stage: str,
    ) -> dict[str, Any] | None:
        return (
            self.config.injected_failures.get(key, {}).get(stage)
            if self.config.injected_failures
            else None
        )

    def _inverted_stage(self, pool: LocalWorkerPool) -> None:
        assert self.state is not None
        for wave in self._decision_waves():
            pending = [
                decision
                for decision in wave
                if decision["kind"] == "doc"
                and not self.state.has_unit("inverted", decision["batch_key"])
            ]
            for decision in wave:
                if (
                    decision["kind"] == "doc"
                    and self.state.has_unit("inverted", decision["batch_key"])
                ):
                    self.state.mark_reused("inverted", decision["batch_key"])
            if not pending:
                continue
            tasks: list[dict[str, Any]] = []
            for decision in pending:
                documents = _doc_decision_documents(self.ingest, decision)
                tasks.extend(
                    build_statistics_tasks(
                        documents,
                        weights=self.weights,
                        chunk_size=self.config.chunk_size,
                        compensated=self.config.compensated,
                        decimals=self.config.decimals,
                        injected_failure=self._injected_failure(
                            decision["batch_key"], "inverted"
                        ),
                    )
                )
            envelopes = pool.map(tasks)
            successes, failures = merge_worker_envelopes(envelopes)
            success_by_key: dict[str, list[dict[str, Any]]] = {}
            for envelope in successes:
                success_by_key.setdefault(str(envelope["batch_key"]), []).append(
                    envelope["payload"]
                )
            for decision in pending:
                key = decision["batch_key"]
                if key in failures:
                    self.state.mark_failed("inverted", key, failures[key])
                    self._commit("inverted")
                    continue
                chunks = sorted(
                    success_by_key.get(key, []),
                    key=lambda item: int(item["chunk_index"]),
                )
                if not chunks:
                    self.state.mark_failed(
                        "inverted",
                        key,
                        error_dict(
                            E_WORKER,
                            "inverted",
                            "worker returned no statistics chunks",
                            batch_no=int(decision["batch_no"]),
                            batch_kind="doc",
                        ),
                    )
                    self._commit("inverted")
                    continue
                batch_no = int(decision["batch_no"])
                partition, postings = merge_chunks(
                    chunks,
                    batch_no=batch_no,
                    compensated=self.config.compensated,
                    decimals=self.config.decimals,
                )
                self.state.payload["partitions"][str(batch_no)] = partition
                self.state.payload["batch_postings"][str(batch_no)] = postings
                self.state.payload["global_stats"] = merge_global_partitions(
                    self.state.payload["partitions"].values(),
                    compensated=self.config.compensated,
                    decimals=self.config.decimals,
                )
                self.state.mark_completed("inverted", key)
                self._commit("inverted")
                self._sync_statistics_snapshot()

    def _projection_stage(self, pool: LocalWorkerPool) -> None:
        assert self.state is not None
        for wave in self._decision_waves():
            pending = [
                decision
                for decision in wave
                if decision["kind"] == "doc"
                and not self.state.has_unit("projection", decision["batch_key"])
            ]
            for decision in wave:
                if (
                    decision["kind"] == "doc"
                    and self.state.has_unit("projection", decision["batch_key"])
                ):
                    self.state.mark_reused("projection", decision["batch_key"])
            if not pending:
                continue
            tasks: list[dict[str, Any]] = []
            for decision in pending:
                documents = _doc_decision_documents(self.ingest, decision)
                tasks.extend(
                    build_projection_tasks(
                        documents,
                        weights=self.weights,
                        chunk_size=self.config.chunk_size,
                        decimals=self.config.decimals,
                        subnormal_floor=self.config.subnormal_floor,
                        injected_failure=self._injected_failure(
                            decision["batch_key"], "projection"
                        ),
                    )
                )
            envelopes = pool.map(tasks)
            successes, failures = merge_worker_envelopes(envelopes)
            success_by_key: dict[str, list[dict[str, Any]]] = {}
            for envelope in successes:
                success_by_key.setdefault(str(envelope["batch_key"]), []).append(
                    envelope["payload"]
                )
            for decision in pending:
                key = decision["batch_key"]
                if key in failures:
                    self.state.mark_failed("projection", key, failures[key])
                    self._commit("projection")
                    continue
                chunks = success_by_key.get(key, [])
                if not chunks:
                    self.state.mark_failed(
                        "projection",
                        key,
                        error_dict(
                            E_WORKER,
                            "projection",
                            "worker returned no projection chunks",
                            batch_no=int(decision["batch_no"]),
                            batch_kind="doc",
                        ),
                    )
                    self._commit("projection")
                    continue
                vectors = merge_document_chunks(chunks)
                expected_ids = execution_ids(
                    decision,
                    self.ingest.doc_batches[int(decision["batch_no"])],
                )
                actual_ids = [str(vector["doc_id"]) for vector in vectors]
                if actual_ids != expected_ids:
                    self.state.mark_failed(
                        "projection",
                        key,
                        error_dict(
                            E_INCONSISTENT,
                            "projection",
                            "projected document IDs differ from the execution plan",
                            batch_no=int(decision["batch_no"]),
                            batch_kind="doc",
                        ),
                    )
                else:
                    for vector in vectors:
                        self.state.payload["vectors"][str(vector["doc_id"])] = vector
                    self.state.mark_completed("projection", key)
                self._commit("projection")

    def _scoring_stage(self) -> None:
        assert self.state is not None
        doc_vectors = [
            self.state.payload["vectors"][doc_id]
            for doc_id in sorted(self.state.payload["vectors"])
        ]
        for decision in accepted_decisions(self.plan):
            if decision["kind"] != "query":
                continue
            key = decision["batch_key"]
            if self.state.has_unit("scoring", key):
                self.state.mark_reused("scoring", key)
                continue
            executed_top_k = int(decision["execution"]["top_k"])
            for query in _query_decision_queries(self.ingest, decision):
                result, vector = score_query(
                    query,
                    doc_vectors,
                    self.weights,
                    executed_top_k=executed_top_k,
                    threshold=self.config.threshold,
                    compare_mode=self.config.compare_mode,
                    tolerance=self.config.tolerance,
                    decimals=self.config.decimals,
                    subnormal_floor=self.config.subnormal_floor,
                )
                self.state.payload["query_vectors"][query.query_id] = vector
                self.state.payload["results"][query.query_id] = result
            self.state.mark_completed("scoring", key)
            self._commit("scoring")

    def _write_outputs(self) -> dict[str, str]:
        assert self.state is not None
        self.output_dir.mkdir(parents=True, exist_ok=True)
        vectors = [
            self.state.payload["vectors"][doc_id]
            for doc_id in sorted(
                self.state.payload["vectors"],
                key=lambda item: (
                    int(self.state.payload["vectors"][item]["batch_no"]),
                    item,
                ),
            )
        ]
        results = [
            self.state.payload["results"][query_id]
            for query_id in sorted(
                self.state.payload["results"],
                key=lambda item: (
                    int(self.state.payload["results"][item]["batch_no"]),
                    item,
                ),
            )
        ]
        validation = sorted(
            self.ingest.validation,
            key=lambda row: (
                0 if row.get("status") == "error" else 1,
                row.get("batch_no")
                if isinstance(row.get("batch_no"), int)
                else 2**62,
                str(row.get("batch_kind") or ""),
                str(row.get("record_id") or ""),
                int(row.get("line") or 0),
            ),
        )
        plan_rows = []
        for decision in self.plan["decisions"]:
            plan_rows.append(decision)
        worker_failures = []
        for stage in ("ingest", "inverted", "projection", "scoring"):
            for key in sorted(self.state.failed[stage]):
                error = dict(self.state.failed[stage][key])
                error["stage"] = stage
                error["batch_key"] = key
                worker_failures.append(error)
        worker_failures.sort(
            key=lambda item: (str(item.get("stage")), str(item.get("batch_key")))
        )

        paths = {
            "vectors.jsonl": self.output_dir / "vectors.jsonl",
            "results.jsonl": self.output_dir / "results.jsonl",
            "validation.jsonl": self.output_dir / "validation.jsonl",
            "statistics.json": self.output_dir / "statistics.json",
            "inverted_index.json": self.output_dir / "inverted_index.json",
            "plan.json": self.output_dir / "plan.json",
            "failures.jsonl": self.output_dir / "failures.jsonl",
        }
        write_jsonl_atomic(paths["vectors.jsonl"], vectors)
        write_jsonl_atomic(paths["results.jsonl"], results)
        write_jsonl_atomic(paths["validation.jsonl"], validation)
        write_json_atomic(
            paths["statistics.json"],
            {
                "global": self.state.payload["global_stats"],
                "partitions": [
                    self.state.payload["partitions"][key]
                    for key in sorted(self.state.payload["partitions"], key=int)
                ],
                "reduction_order": "batch_no,chunk_index,doc_id,term",
                "compensated": self.config.compensated,
            },
        )
        write_json_atomic(
            paths["inverted_index.json"],
            {
                "global": self.state.payload["global_stats"],
                "partitions": [
                    self.state.payload["partitions"][key]
                    for key in sorted(self.state.payload["partitions"], key=int)
                ],
                "postings": _global_postings(self.state.payload),
            },
        )
        write_json_atomic(paths["plan.json"], self.plan)
        write_jsonl_atomic(paths["failures.jsonl"], worker_failures)
        return {name: sha256_file(path) for name, path in sorted(paths.items())}

    def run(self) -> PipelineResult:
        self._check_existing_plan()
        self._load_or_initialize()
        assert self.state is not None
        self._sync_statistics_snapshot()
        self._ingest_stage()
        with LocalWorkerPool(
            self.config.workers,
            use_process_pool=self.config.use_process_pool,
        ) as pool:
            self._inverted_stage(pool)
            self._projection_stage(pool)
        self._scoring_stage()
        self._sync_statistics_snapshot()

        consistency = verify_consistency(
            ingest=self.ingest,
            plan=self.plan,
            payload=self.state.payload,
            completed=self.state.completed,
            failed=self.state.failed,
            weights=self.weights,
            chunk_size=self.config.chunk_size,
            compensated=self.config.compensated,
            decimals=self.config.decimals,
            subnormal_floor=self.config.subnormal_floor,
            threshold=self.config.threshold,
            compare_mode=self.config.compare_mode,
            tolerance=self.config.tolerance,
            recomputed=self.state.recomputed,
            reused=self.state.reused,
        )
        write_json_atomic(self.output_dir / "consistency.json", consistency)
        artifact_hashes = self._write_outputs()
        artifact_hashes["consistency.json"] = sha256_file(
            self.output_dir / "consistency.json"
        )

        recovery = {
            "schema": "sparse-recall.recovery-report.v1",
            "resumed": self.resumed,
            "initial_checkpoint_version": self.initial_version,
            "final_checkpoint_version": self.version,
            "commits_this_run": self.commits_this_run,
            "reused_batches": {
                stage: len(values) for stage, values in sorted(self.state.reused.items())
            },
            "recomputed_batches": {
                stage: len(values)
                for stage, values in sorted(self.state.recomputed.items())
            },
            "reused_batch_keys": {
                stage: sorted(values)
                for stage, values in sorted(self.state.reused.items())
            },
            "recomputed_batch_keys": {
                stage: sorted(values)
                for stage, values in sorted(self.state.recomputed.items())
            },
        }
        write_json_atomic(self.workdir / "recovery_report.json", recovery)

        report = {
            "schema": "sparse-recall.run-report.v1",
            "status": "ok",
            "environment": {
                "python": sys.version.split()[0],
                "process_pool": pool.start_method,
                "platform": sys.platform,
            },
            "inputs": {
                "documents": len(self.ingest.documents),
                "queries": len(self.ingest.queries),
                "input_sha256": self.inputs_sha256,
            },
            "plan": self.plan["totals"],
            "checkpoint": {
                "final_version": self.version,
                "resumed": self.resumed,
                "initial_version": self.initial_version,
            },
            "verification": consistency,
            "artifact_sha256": artifact_hashes,
            "artifacts": {
                "output_dir": str(self.output_dir),
                "plan": str(self.plan_path),
                "checkpoint": str(self.checkpoint_store.path),
                "statistics_snapshot": str(self.statistics_store.path),
                "recovery_report": str(self.workdir / "recovery_report.json"),
            },
        }
        write_json_atomic(self.workdir / "run_report.json", report)
        return PipelineResult(
            workdir=self.workdir,
            output_dir=self.output_dir,
            report=report,
        )


def run_pipeline(
    *,
    document_path: str | Path,
    query_path: str | Path,
    weights_path: str | Path,
    workdir: str | Path,
    scheduler_config: SchedulerConfig,
    config: PipelineConfig | None = None,
) -> PipelineResult:
    pipeline = RecallPipeline(
        document_path=document_path,
        query_path=query_path,
        weights_path=weights_path,
        workdir=workdir,
        scheduler_config=scheduler_config,
        config=config or PipelineConfig(),
    )
    return pipeline.run()
