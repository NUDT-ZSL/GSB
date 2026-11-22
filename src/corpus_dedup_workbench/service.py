from __future__ import annotations

import copy
import json
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .cache import ContentAddressedCache
from .config import WorkbenchConfig
from .errors import (
    E_DATA_INVALID,
    E_DOC_NOT_FOUND,
    E_INCONSISTENT,
    E_LATENCY_BUDGET,
    E_RESOURCE_LIMIT,
    WorkbenchError,
)
from .pipeline import merge_shard_artifacts, process_shard
from .signatures import build_signature_record
from .snapshots import SnapshotManager
from .util import (
    content_hash,
    rounded_float,
)

SNAPSHOT_STATS = "shard_statistics"
SNAPSHOT_REMOVED = "removed_list"


@dataclass
class RuntimeState:
    result: dict[str, Any]
    config: WorkbenchConfig
    source_docs: dict[str, dict[str, Any]]
    docs_by_id: dict[str, dict[str, Any]]
    candidate_by_id: dict[str, dict[str, Any]]
    bucket_by_doc: dict[str, dict[str, Any]]
    cache_keys: dict[str, str]
    cache_report: dict[str, Any]


class DedupService:
    def __init__(
        self,
        data_path: str | Path,
        config: WorkbenchConfig,
        cache_root: str | Path | None = None,
        initialize: bool = True,
    ) -> None:
        config.validate()
        self.data_path = Path(data_path)
        self.config = config
        self.cache = ContentAddressedCache(config.max_cache_entries, cache_root)
        self.snapshots = SnapshotManager()
        self._lock = threading.RLock()
        self._source_docs = self._load_docs()
        self._runtime: RuntimeState | None = None
        self._query_faults: dict[str, dict[str, Any]] = {}
        self._last_transition: dict[str, Any] | None = None
        if initialize:
            with self._lock:
                self._runtime = self._build_runtime(self.config)

    def _load_docs(self) -> dict[str, dict[str, Any]]:
        if not self.data_path.exists():
            raise WorkbenchError(
                E_DATA_INVALID,
                "语料清单文件不存在",
                stage="ingest_validate",
                details={"path": str(self.data_path)},
            )
        docs: dict[str, dict[str, Any]] = {}
        required = {
            "shard_id",
            "doc_id",
            "text",
            "lang",
            "source",
            "token_estimate",
        }
        with self.data_path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    doc = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise WorkbenchError(
                        E_DATA_INVALID,
                        "JSONL 行解析失败",
                        stage="ingest_validate",
                        details={"line": line_number, "reason": str(exc)},
                    ) from exc
                missing = sorted(required - set(doc))
                if missing:
                    raise WorkbenchError(
                        E_DATA_INVALID,
                        "文档缺少必填字段",
                        stage="ingest_validate",
                        details={
                            "line": line_number,
                            "doc_id": doc.get("doc_id"),
                            "missing": missing,
                        },
                    )
                if not isinstance(doc["text"], str):
                    raise WorkbenchError(
                        E_DATA_INVALID,
                        "text 必须为字符串",
                        stage="ingest_validate",
                        details={"line": line_number, "doc_id": doc["doc_id"]},
                    )
                if doc["doc_id"] in docs:
                    raise WorkbenchError(
                        E_DATA_INVALID,
                        "doc_id 重复",
                        stage="ingest_validate",
                        details={"line": line_number, "doc_id": doc["doc_id"]},
                    )
                docs[doc["doc_id"]] = doc
        if not docs:
            raise WorkbenchError(
                E_DATA_INVALID,
                "语料清单为空",
                stage="ingest_validate",
            )
        return docs

    def _group_by_shard(self) -> dict[str, list[dict[str, Any]]]:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for doc in self._source_docs.values():
            grouped[doc["shard_id"]].append(doc)
        for docs in grouped.values():
            docs.sort(key=lambda item: item["doc_id"])
        return dict(sorted(grouped.items()))

    def _shard_cache_key(
        self,
        shard_id: str,
        shard_content_hash: str,
        config: WorkbenchConfig,
        pair_limit: int | None = None,
    ) -> str:
        descriptor = {
            "kind": "shard-dedup-artifact-v1",
            "shard_id": shard_id,
            "shard_content_hash": shard_content_hash,
            "pair_limit": (
                config.max_candidate_comparisons
                if pair_limit is None
                else pair_limit
            ),
            **config.cache_descriptor(),
        }
        return "shard:" + content_hash(descriptor)

    def _publish_snapshots(self, result: dict[str, Any]) -> None:
        stats_payload = {
            "schema_version": result["schema_version"],
            "config_hash": result["config_hash"],
            "totals": result["totals"],
            "shard_stats": result["shard_stats"],
            "shard_content_hashes": result["shard_content_hashes"],
        }
        removed_payload = {
            "schema_version": result["schema_version"],
            "config_hash": result["config_hash"],
            "removed": result["removed"],
        }
        self.snapshots.publish(SNAPSHOT_STATS, stats_payload)
        self.snapshots.publish(SNAPSHOT_REMOVED, removed_payload)

    def _build_runtime(
        self,
        config: WorkbenchConfig,
        force_recompute: bool = False,
    ) -> RuntimeState:
        if len(self._source_docs) > config.max_docs_per_batch:
            raise WorkbenchError(
                E_RESOURCE_LIMIT,
                "单批文档数超过配置上限",
                stage="ingest_validate",
                details={
                    "doc_count": len(self._source_docs),
                    "max_docs_per_batch": config.max_docs_per_batch,
                },
            )

        artifacts: list[dict[str, Any]] = []
        cache_keys: dict[str, str] = {}
        recomputed_keys: list[str] = []
        reused_keys: list[str] = []
        recomputed_docs: list[str] = []
        reused_docs: list[str] = []
        shard_details: list[dict[str, Any]] = []
        remaining_pair_limit = config.max_candidate_comparisons
        evictions_before = self.cache.snapshot_stats()["evictions"]
        for shard_id, docs in self._group_by_shard().items():
            shard_hash = content_hash(docs)
            key = self._shard_cache_key(
                shard_id,
                shard_hash,
                config,
                pair_limit=remaining_pair_limit,
            )
            cache_keys[shard_id] = key
            artifact = None if force_recompute else self.cache.get(key)
            if artifact is not None:
                reused_keys.append(key)
                reused_docs.extend(doc["doc_id"] for doc in docs)
                status = "reused"
            else:
                artifact = process_shard(
                    shard_id,
                    docs,
                    config,
                    pair_limit=remaining_pair_limit,
                )
                metadata = {
                    "kind": "shard-dedup-artifact",
                    "shard_id": shard_id,
                    "shard_content_hash": shard_hash,
                    "config_hash": config.config_hash,
                    "doc_ids": [doc["doc_id"] for doc in docs],
                    "validation_mode": config.validation_mode,
                    "pair_limit": remaining_pair_limit,
                }
                self.cache.put(key, artifact, metadata)
                recomputed_keys.append(key)
                recomputed_docs.extend(doc["doc_id"] for doc in docs)
                status = "recomputed"
            artifacts.append(artifact)
            remaining_pair_limit = max(
                0,
                remaining_pair_limit
                - artifact["stats"]["candidate_pair_count"],
            )
            shard_details.append(
                {
                    "shard_id": shard_id,
                    "cache_key": key,
                    "shard_content_hash": shard_hash,
                    "status": status,
                    "pair_limit": remaining_pair_limit,
                }
            )

        result = merge_shard_artifacts(artifacts, config)
        docs_by_id = result["docs"]
        candidate_by_id = {
            candidate["candidate_id"]: candidate
            for candidate in result["candidate_pairs"]
        }
        bucket_by_doc: dict[str, dict[str, Any]] = {}
        for bucket in result["buckets"]:
            for doc_id in bucket["doc_ids"]:
                bucket_by_doc[doc_id] = bucket
        cache_report = {
            "recomputed_keys": sorted(recomputed_keys),
            "reused_keys": sorted(reused_keys),
            "recomputed_docs": sorted(recomputed_docs),
            "reused_docs": sorted(reused_docs),
            "recomputed_shards": [
                item["shard_id"]
                for item in shard_details
                if item["status"] == "recomputed"
            ],
            "reused_shards": [
                item["shard_id"]
                for item in shard_details
                if item["status"] == "reused"
            ],
            "shards": shard_details,
            "cache_stats": self.cache.snapshot_stats(),
            "degradation": (
                {"degraded": True, "category": "cache_lru_eviction"}
                if (
                    self.cache.snapshot_stats()["evictions"]
                    - evictions_before
                    > 0
                )
                else None
            ),
        }
        result["cache_report"] = cache_report
        self._publish_snapshots(result)
        result["snapshots"] = self.snapshots.metadata()
        return RuntimeState(
            result=result,
            config=config,
            source_docs=self._source_docs,
            docs_by_id=docs_by_id,
            candidate_by_id=candidate_by_id,
            bucket_by_doc=bucket_by_doc,
            cache_keys=cache_keys,
            cache_report=cache_report,
        )

    def batch(self) -> dict[str, Any]:
        with self._lock:
            if self._runtime is None:
                self._runtime = self._build_runtime(self.config)
            return copy.deepcopy(self._runtime.result)

    def refresh(self) -> dict[str, Any]:
        with self._lock:
            self._runtime = self._build_runtime(self.config)
            return copy.deepcopy(self._runtime.result)

    def reconfigure(self, updates: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            old_runtime = self._runtime
            new_config = self.config.replaced(updates)
            if new_config == self.config:
                return {
                    "ok": True,
                    "changed": False,
                    "transition": {
                        "changed_fields": [],
                        "affected_docs": [],
                        "affected_cache_entries": [],
                        "invalidated_entries": [],
                        "recomputed_docs": [],
                        "reused_docs": sorted(
                            self._source_docs
                        ),
                    },
                    "cache_report": old_runtime.cache_report if old_runtime else {},
                    "snapshots": self.snapshots.metadata(),
                }
            affected_docs, changed_fields, affected_entries = self._affected_scope(
                old_runtime, new_config
            )
            old_keys = (
                list(old_runtime.cache_keys.values())
                if old_runtime is not None
                else []
            )
            invalidated = self.cache.invalidate(old_keys)
            previous_cache_limit = self.cache.max_entries
            self.cache.max_entries = new_config.max_cache_entries
            previous_config = self.config
            self.config = new_config
            try:
                new_runtime = self._build_runtime(new_config)
            except Exception:
                self.config = previous_config
                self.cache.max_entries = previous_cache_limit
                raise
            self._runtime = new_runtime
            transition = {
                "changed_fields": changed_fields,
                "affected_docs": sorted(affected_docs),
                "affected_cache_entries": sorted(affected_entries),
                "invalidated_entries": invalidated,
                "recomputed_docs": new_runtime.cache_report["recomputed_docs"],
                "reused_docs": new_runtime.cache_report["reused_docs"],
                "recomputed_keys": new_runtime.cache_report["recomputed_keys"],
                "reused_keys": new_runtime.cache_report["reused_keys"],
                "semantic_note": (
                    "缓存键包含分片内容哈希、签名长度、阈值、窗口与校验模式；"
                    "配置变化使旧键不可复用，因此受影响分片整体重算。"
                ),
            }
            self._last_transition = transition
            return {
                "ok": True,
                "changed": True,
                "transition": transition,
                "cache_report": new_runtime.cache_report,
                "snapshots": self.snapshots.metadata(),
            }

    def _affected_scope(
        self,
        old_runtime: RuntimeState | None,
        new_config: WorkbenchConfig,
    ) -> tuple[set[str], list[str], list[str]]:
        old = self.config.to_dict()
        new = new_config.to_dict()
        changed_fields = sorted(
            key
            for key in old
            if key != "sample" and old.get(key) != new.get(key)
        )
        if old_runtime is None:
            return set(self._source_docs), changed_fields, []
        affected_docs: set[str] = set()
        all_doc_ids = set(old_runtime.docs_by_id)
        all_stage_fields = {
            "signature_length",
            "summary_bands",
            "values_per_band",
            "normalization_version",
        }
        comparison_fields = {
            "similarity_threshold",
            "threshold_policy",
            "threshold_tolerance",
            "validation_mode",
            "digest_min_common_bands",
        }
        if all_stage_fields & set(changed_fields):
            affected_docs = set(all_doc_ids)
        if comparison_fields & set(changed_fields):
            affected_docs.update(
                doc_id
                for doc_id, doc in old_runtime.docs_by_id.items()
                if doc["candidate_ids"]
            )
        if "candidate_window" in changed_fields:
            for bucket in old_runtime.result["buckets"]:
                if bucket["size"] > 1:
                    affected_docs.update(bucket["doc_ids"])
        if {
            "max_docs_per_batch",
            "max_candidate_comparisons",
            "latency_budget_ms",
            "resource_policy",
        } & set(changed_fields):
            affected_docs.update(all_doc_ids)
        affected_entries = sorted(old_runtime.cache_keys.values())
        return affected_docs, changed_fields, affected_entries

    def _effective_budget(
        self,
        requested_ms: float | None,
        configured_ms: float,
    ) -> float:
        configured = float(configured_ms)
        if requested_ms is None:
            return configured
        if not isinstance(requested_ms, (int, float)) or float(requested_ms) <= 0:
            raise WorkbenchError(
                E_LATENCY_BUDGET,
                "查询延迟预算必须为正数",
                stage="query_budget",
                details={"requested_ms": requested_ms, "budget_ceiling_ms": configured},
            )
        return min(float(requested_ms), configured)

    def query(
        self,
        doc_id: str,
        latency_budget_ms: float | None = None,
    ) -> dict[str, Any]:
        start_ns = time.perf_counter_ns()
        with self._lock:
            runtime = self._runtime
            fault = copy.deepcopy(self._query_faults.get(doc_id))
        if runtime is None:
            raise WorkbenchError(
                E_DOC_NOT_FOUND,
                "服务尚未物化批量结果",
                stage="query_lookup",
            )
        if doc_id not in runtime.docs_by_id:
            raise WorkbenchError(
                E_DOC_NOT_FOUND,
                "doc_id 不存在",
                stage="query_lookup",
                details={"doc_id": doc_id},
            )

        runtime_config = runtime.config
        budget_ms = self._effective_budget(
            latency_budget_ms,
            runtime_config.latency_budget_ms,
        )
        completed_stages: list[dict[str, Any]] = []
        doc_record = copy.deepcopy(runtime.docs_by_id[doc_id])
        candidate_count = len(doc_record["candidate_ids"])
        block_count = len(doc_record["block_signatures"])
        estimated_ms = round(
            0.015 + block_count * 0.0015 + candidate_count * 0.003,
            6,
        )

        def finish_stage(name: str, stage_start_ns: int) -> None:
            elapsed = (time.perf_counter_ns() - stage_start_ns) / 1_000_000
            completed_stages.append({"name": name, "elapsed_ms": round(elapsed, 6)})
            total_elapsed = (time.perf_counter_ns() - start_ns) / 1_000_000
            if total_elapsed > budget_ms:
                raise WorkbenchError(
                    E_LATENCY_BUDGET,
                    "查询超过实际处理延迟预算",
                    stage=name,
                    details={
                        "doc_id": doc_id,
                        "budget_ms": budget_ms,
                        "budget_ceiling_ms": float(runtime_config.latency_budget_ms),
                        "actual_elapsed_ms": round(total_elapsed, 6),
                        "estimated_ms": estimated_ms,
                        "estimate_basis": {
                            "signature_blocks": block_count,
                            "candidate_comparisons": candidate_count,
                            "formula": "0.015 + blocks*0.0015 + candidates*0.003",
                        },
                        "candidate_comparisons_skipped": 0,
                    },
                    completed_stages=copy.deepcopy(completed_stages),
                )

        # Stage 1: lookup the batch-side immutable record.
        stage_start = time.perf_counter_ns()
        batch_doc = doc_record
        finish_stage("query_lookup", stage_start)

        # Stage 2: independently regenerate signatures from source text.
        stage_start = time.perf_counter_ns()
        source_doc = runtime.source_docs[doc_id]
        regenerated = build_signature_record(source_doc, runtime_config)
        finish_stage("signature_verify", stage_start)

        # Stage 3: reconstruct the complete candidate trace from the batch index.
        stage_start = time.perf_counter_ns()
        expected_candidates = sorted(
            (
                copy.deepcopy(candidate)
                for candidate in runtime.candidate_by_id.values()
                if candidate["a_doc"] == doc_id or candidate["b_doc"] == doc_id
            ),
            key=lambda item: item["candidate_id"],
        )
        expected_candidate_ids = [
            candidate["candidate_id"] for candidate in expected_candidates
        ]
        query = {
            "ok": True,
            "doc_id": doc_id,
            "shard_id": batch_doc["shard_id"],
            "block_signatures": list(regenerated["block_signatures"]),
            "blocks": copy.deepcopy(regenerated["blocks"]),
            "summary": copy.deepcopy(regenerated["summary"]),
            "bucket_key": regenerated["bucket_key"],
            "bucket": {
                "bucket_key": runtime.bucket_by_doc[doc_id]["bucket_key"],
                "doc_ids": list(runtime.bucket_by_doc[doc_id]["doc_ids"]),
                "size": runtime.bucket_by_doc[doc_id]["size"],
                "candidate_count": runtime.bucket_by_doc[doc_id][
                    "candidate_count"
                ],
                "empty_candidates": runtime.bucket_by_doc[doc_id][
                    "empty_candidates"
                ],
            },
            "candidate_ids": list(batch_doc["candidate_ids"]),
            "candidate_trace": copy.deepcopy(batch_doc["candidate_trace"]),
            "similar_decision": copy.deepcopy(batch_doc["similar_decision"]),
            "removed": batch_doc["removed"],
            "removal_reason": copy.deepcopy(batch_doc["removal_reason"]),
            "quality_score": batch_doc["quality_score"],
            "content_hash": regenerated["content_hash"],
            "consistency": {
                "checked": True,
                "precision": "1e-12",
                "fields": [
                    "block_signatures",
                    "summary",
                    "candidate_trace",
                    "candidate_set",
                    "bucket_partition",
                    "similar_decision",
                    "removal_reason",
                ],
            },
            "latency": {
                "budget_ms": budget_ms,
                "budget_ceiling_ms": float(runtime_config.latency_budget_ms),
                "estimated_ms": estimated_ms,
                "estimate_basis": {
                    "signature_blocks": block_count,
                    "candidate_comparisons": candidate_count,
                    "formula": "0.015 + blocks*0.0015 + candidates*0.003",
                },
                "candidate_comparisons_skipped": 0,
            },
            "snapshots": copy.deepcopy(runtime.result.get("snapshots", {})),
        }
        if fault:
            self._apply_query_fault(query, fault)
        finish_stage("candidate_trace", stage_start)

        # Stage 4: compare query-side and batch-side views field by field.
        stage_start = time.perf_counter_ns()
        self._assert_consistency(
            query=query,
            batch_doc=batch_doc,
            regenerated=regenerated,
            expected_candidate_ids=expected_candidate_ids,
            runtime=runtime,
        )
        finish_stage("consistency_check", stage_start)

        total_elapsed_ms = round(
            (time.perf_counter_ns() - start_ns) / 1_000_000,
            6,
        )
        query["latency"].update(
            {
                "actual_ms": total_elapsed_ms,
                "stages": copy.deepcopy(completed_stages),
                "within_budget": total_elapsed_ms <= budget_ms,
            }
        )
        return query

    def _apply_query_fault(
        self, query: dict[str, Any], fault: dict[str, Any]
    ) -> None:
        stage = fault.get("stage")
        if stage == "block_signatures" and query["block_signatures"]:
            query["block_signatures"][0] = str(
                fault.get("value", "0000000000000000")
            )
        elif stage == "candidate_trace":
            if query["candidate_trace"]:
                query["candidate_trace"][0]["similarity"] = float(
                    fault.get("value", 0.123456789)
                )
            else:
                query["candidate_trace"].append(
                    {
                        "candidate_id": "fault-candidate",
                        "peer_doc_id": "fault-peer",
                    }
                )
        elif stage == "similar_decision":
            query["similar_decision"]["outcome"] = str(
                fault.get("value", "fault_outcome")
            )
        elif stage == "removal_reason" and query["removal_reason"] is not None:
            query["removal_reason"]["reason"] = str(
                fault.get("value", "fault_reason")
            )
        elif stage == "bucket_partition":
            query["bucket"]["doc_ids"] = sorted(
                [*query["bucket"]["doc_ids"], "fault-doc"]
            )

    def _normalized(self, value: Any) -> Any:
        if isinstance(value, float):
            return rounded_float(value, 12)
        if isinstance(value, dict):
            return {
                key: self._normalized(item)
                for key, item in sorted(value.items())
            }
        if isinstance(value, list):
            return [self._normalized(item) for item in value]
        return value

    def _find_first_diff(
        self,
        left: Any,
        right: Any,
        path: str = "$",
    ) -> dict[str, Any] | None:
        left = self._normalized(left)
        right = self._normalized(right)
        if type(left) is not type(right):
            return {
                "path": path,
                "query_value": left,
                "batch_value": right,
            }
        if isinstance(left, dict):
            if set(left) != set(right):
                return {
                    "path": path,
                    "query_value": sorted(left),
                    "batch_value": sorted(right),
                }
            for key in sorted(left):
                difference = self._find_first_diff(
                    left[key], right[key], f"{path}.{key}"
                )
                if difference:
                    return difference
            return None
        if isinstance(left, list):
            if len(left) != len(right):
                return {
                    "path": path,
                    "query_value": len(left),
                    "batch_value": len(right),
                }
            for index, (left_item, right_item) in enumerate(zip(left, right)):
                difference = self._find_first_diff(
                    left_item, right_item, f"{path}[{index}]"
                )
                if difference:
                    return difference
            return None
        if left != right:
            return {
                "path": path,
                "query_value": left,
                "batch_value": right,
            }
        return None

    def _assert_consistency(
        self,
        query: dict[str, Any],
        batch_doc: dict[str, Any],
        regenerated: dict[str, Any],
        expected_candidate_ids: list[str],
        runtime: RuntimeState,
    ) -> None:
        comparisons = [
            (
                "block_signatures",
                query["block_signatures"],
                batch_doc["block_signatures"],
            ),
            (
                "block_signatures_regenerated",
                query["block_signatures"],
                regenerated["block_signatures"],
            ),
            ("summary", query["summary"], batch_doc["summary"]),
            ("summary_regenerated", query["summary"], regenerated["summary"]),
            ("candidate_trace", query["candidate_trace"], batch_doc["candidate_trace"]),
            ("candidate_set", query["candidate_ids"], expected_candidate_ids),
            (
                "bucket_partition",
                {
                    "bucket_key": query["bucket_key"],
                    "doc_ids": query["bucket"]["doc_ids"],
                },
                {
                    "bucket_key": batch_doc["bucket_key"],
                    "doc_ids": runtime.bucket_by_doc[query["doc_id"]]["doc_ids"],
                },
            ),
            (
                "similar_decision",
                query["similar_decision"],
                batch_doc["similar_decision"],
            ),
            (
                "removal_reason",
                query["removal_reason"],
                batch_doc["removal_reason"],
            ),
        ]
        for stage, query_value, batch_value in comparisons:
            difference = self._find_first_diff(query_value, batch_value)
            if difference:
                raise WorkbenchError(
                    E_INCONSISTENT,
                    "查询侧与批量侧结果不一致",
                    stage=stage,
                    details={
                        "first_diff_doc_id": query["doc_id"],
                        "stage": stage,
                        "path": difference["path"],
                        "query_value": difference["query_value"],
                        "batch_value": difference["batch_value"],
                        "precision": "1e-12",
                    },
                )
        query["consistency"].update(
            {
                "candidate_set_match": True,
                "bucket_match": True,
                "block_signature_match": True,
            }
        )

    def validate_all_consistency(self) -> dict[str, Any]:
        with self._lock:
            runtime = self._runtime
            if runtime is None:
                raise WorkbenchError(
                    E_DOC_NOT_FOUND,
                    "服务尚未物化批量结果",
                    stage="consistency_check",
                )
            doc_ids = sorted(runtime.docs_by_id)
        checked = 0
        max_actual_ms = 0.0
        for doc_id in doc_ids:
            result = self.query(doc_id)
            checked += 1
            max_actual_ms = max(max_actual_ms, result["latency"]["actual_ms"])
        return {
            "ok": True,
            "checked_docs": checked,
            "all_consistent": True,
            "precision": "1e-12",
            "max_actual_query_ms": max_actual_ms,
        }

    def compare_modes(self, threshold_overrides: dict[str, Any] | None = None) -> dict[str, Any]:
        overrides = threshold_overrides or {}
        full_config = self.config.replaced(
            {"validation_mode": "full", **overrides}
        )
        digest_config = self.config.replaced(
            {"validation_mode": "digest_summary", **overrides}
        )
        full_artifacts = [
            process_shard(shard_id, docs, full_config)
            for shard_id, docs in self._group_by_shard().items()
        ]
        digest_artifacts = [
            process_shard(shard_id, docs, digest_config)
            for shard_id, docs in self._group_by_shard().items()
        ]
        full = merge_shard_artifacts(full_artifacts, full_config)
        digest = merge_shard_artifacts(digest_artifacts, digest_config)
        full_removed = {
            item["doc_id"]: item for item in full["removed"]
        }
        digest_removed = {
            item["doc_id"]: item for item in digest["removed"]
        }
        full_ids = set(full_removed)
        digest_ids = set(digest_removed)
        only_full = sorted(full_ids - digest_ids)
        only_digest = sorted(digest_ids - full_ids)
        digest_pair_by_docs = {
            tuple(sorted((candidate["a_doc"], candidate["b_doc"]))): candidate
            for candidate in digest["candidate_pairs"]
        }
        full_pair_by_docs = {
            tuple(sorted((candidate["a_doc"], candidate["b_doc"]))): candidate
            for candidate in full["candidate_pairs"]
        }
        attribution: dict[str, list[dict[str, Any]]] = {
            "summary_conflict_omission": [],
            "hit_block_reduction": [],
            "other": [],
        }
        for doc_id in only_full:
            reason = full_removed[doc_id]
            pair_key = tuple(
                sorted((reason["doc_id"], reason["neighbor_doc_id"]))
            )
            digest_pair = digest_pair_by_docs.get(pair_key)
            if digest_pair is None:
                attribution["other"].append(
                    {
                        "doc_id": doc_id,
                        "detail": "摘要模式中不存在同桶候选对",
                    }
                )
                continue
            if digest_pair["skipped"] and (
                digest_pair["skip_reason"] == "summary_conflict_omission"
            ):
                category = "summary_conflict_omission"
                detail = "共同摘要带不足，候选对被摘要冲突门控遗漏"
            else:
                category = "hit_block_reduction"
                detail = "仅比较摘要锚点块，命中块减少导致相似度低于阈值"
            attribution[category].append(
                {
                    "doc_id": doc_id,
                    "candidate_id": digest_pair["candidate_id"],
                    "full_similarity": full_removed[doc_id]["similarity"],
                    "digest_similarity": digest_pair["similarity"],
                    "full_hit_positions": reason["hit_positions"],
                    "digest_hit_positions": digest_pair["hit_positions"],
                    "detail": detail,
                }
            )

        hit_differences: list[dict[str, Any]] = []
        for pair_key in sorted(set(full_pair_by_docs) & set(digest_pair_by_docs)):
            full_pair = full_pair_by_docs[pair_key]
            digest_pair = digest_pair_by_docs[pair_key]
            full_hits = sorted(
                {
                    (hit["a_position"], hit["b_position"])
                    for hit in full_pair["hit_positions"]
                }
            )
            digest_hits = sorted(
                {
                    (hit["a_position"], hit["b_position"])
                    for hit in digest_pair["hit_positions"]
                }
            )
            if full_hits != digest_hits:
                hit_differences.append(
                    {
                        "candidate_id": full_pair["candidate_id"],
                        "doc_ids": list(pair_key),
                        "full_hit_positions": full_hits,
                        "digest_hit_positions": digest_hits,
                        "full_similarity": full_pair["similarity"],
                        "digest_similarity": digest_pair["similarity"],
                        "digest_skipped": digest_pair["skipped"],
                        "digest_skip_reason": digest_pair["skip_reason"],
                    }
                )
        return {
            "ok": True,
            "full_mode": {
                "removed": sorted(full_ids),
                "kept_count": len(full["kept"]),
                "candidate_pair_count": len(full["candidate_pairs"]),
                "comparison_count": full["totals"]["comparison_count"],
            },
            "digest_mode": {
                "removed": sorted(digest_ids),
                "kept_count": len(digest["kept"]),
                "candidate_pair_count": len(digest["candidate_pairs"]),
                "comparison_count": digest["totals"]["comparison_count"],
                "skipped_candidate_count": digest["totals"][
                    "skipped_candidate_count"
                ],
            },
            "removed_set_difference": {
                "only_full_mode_removed": only_full,
                "only_digest_mode_removed": only_digest,
                "same_removed_count": len(full_ids & digest_ids),
            },
            "hit_position_differences": hit_differences,
            "attribution": {
                key: value for key, value in attribution.items()
            },
            "attribution_summary": {
                "summary_conflict_omission": len(
                    attribution["summary_conflict_omission"]
                ),
                "hit_block_reduction": len(
                    attribution["hit_block_reduction"]
                ),
                "other": len(attribution["other"]),
                "explanation": (
                    "摘要模式只比较摘要锚点块；共同摘要带不足时按"
                    "summary_conflict_omission 跳过，否则命中块减少可能导致"
                    "相似度下降。"
                ),
            },
        }

    def status(self) -> dict[str, Any]:
        with self._lock:
            runtime = self._runtime
            if runtime is None:
                return {
                    "ok": True,
                    "config": self.config.to_dict(),
                    "snapshots": self.snapshots.metadata(),
                    "cache": self.cache.snapshot_stats(),
                    "initialized": False,
                }
            return {
                "ok": True,
                "initialized": True,
                "config": runtime.config.to_dict(),
                "config_hash": runtime.config.config_hash,
                "totals": runtime.result["totals"],
                "shard_stats": runtime.result["shard_stats"],
                "snapshots": self.snapshots.metadata(),
                "cache": self.cache.snapshot_stats(),
                "cache_report": runtime.cache_report,
                "last_transition": self._last_transition,
                "limits": {
                    "max_docs_per_batch": runtime.config.max_docs_per_batch,
                    "max_candidate_comparisons": runtime.config.max_candidate_comparisons,
                    "max_cache_entries": runtime.config.max_cache_entries,
                    "latency_budget_ms": runtime.config.latency_budget_ms,
                    "resource_policy": runtime.config.resource_policy,
                },
                "degradation": self._degradation_summary(runtime.result),
            }

    def _degradation_summary(
        self, result: dict[str, Any]
    ) -> dict[str, Any] | None:
        categories = []
        for stats in result["shard_stats"].values():
            if stats["degraded"]:
                categories.append(stats["degradation"]["category"])
        cache_degradation = result.get("cache_report", {}).get("degradation")
        if cache_degradation:
            categories.append(cache_degradation["category"])
        if not categories:
            return None
        return {"degraded": True, "categories": sorted(set(categories))}

    def get_snapshot(self, kind: str) -> dict[str, Any]:
        return self.snapshots.get(kind)

    def dashboard(self) -> dict[str, Any]:
        with self._lock:
            runtime = self._runtime
            if runtime is None:
                raise WorkbenchError(
                    E_DOC_NOT_FOUND,
                    "服务尚未物化批量结果",
                    stage="dashboard",
                )
            result = runtime.result
            removed = copy.deepcopy(result["removed"])
            kept = []
            for doc_id in result["kept"]:
                doc = result["docs"][doc_id]
                kept.append(
                    {
                        "doc_id": doc_id,
                        "shard_id": doc["shard_id"],
                        "quality_score": doc["quality_score"],
                        "block_count": len(doc["block_signatures"]),
                        "source": doc["source"],
                    }
                )
            histogram: dict[str, int] = {}
            for stats in result["shard_stats"].values():
                for key, value in stats["signature_distribution"][
                    "block_count_histogram"
                ].items():
                    histogram[key] = histogram.get(key, 0) + value
            candidate_preview = [
                {
                    "candidate_id": candidate["candidate_id"],
                    "a_doc": candidate["a_doc"],
                    "b_doc": candidate["b_doc"],
                    "bucket_key": candidate["bucket_key"],
                    "summary_relation": candidate["summary_relation"],
                    "window_distance": candidate["window_distance"],
                    "matched_blocks": candidate["matched_blocks"],
                    "compared_blocks": candidate["compared_blocks"],
                    "similarity": candidate["similarity"],
                    "near_duplicate": candidate["near_duplicate"],
                    "skipped": candidate["skipped"],
                    "skip_reason": candidate["skip_reason"],
                }
                for candidate in result["candidate_pairs"][:200]
            ]
            bucket_preview = [
                {
                    "bucket_key": bucket["bucket_key"],
                    "shard_id": bucket["shard_id"],
                    "size": bucket["size"],
                    "candidate_count": bucket["candidate_count"],
                    "empty_candidates": bucket["empty_candidates"],
                    "doc_ids": bucket["doc_ids"][:20],
                }
                for bucket in result["buckets"][:100]
            ]
            docs_preview = [
                {
                    "doc_id": doc_id,
                    "shard_id": doc["shard_id"],
                    "quality_score": doc["quality_score"],
                    "block_count": len(doc["block_signatures"]),
                    "candidate_count": len(doc["candidate_ids"]),
                    "removed": doc["removed"],
                    "summary_key": doc["summary"]["key"][:32],
                }
                for doc_id, doc in sorted(result["docs"].items())
            ]
            return {
                "ok": True,
                "config": runtime.config.to_dict(),
                "config_hash": runtime.config.config_hash,
                "totals": copy.deepcopy(result["totals"]),
                "shard_stats": copy.deepcopy(result["shard_stats"]),
                "signature_block_count_histogram": {
                    key: histogram[key] for key in sorted(histogram, key=int)
                },
                "buckets": bucket_preview,
                "bucket_total": len(result["buckets"]),
                "candidates": candidate_preview,
                "candidate_total": len(result["candidate_pairs"]),
                "removed": removed,
                "kept": kept,
                "docs": docs_preview,
                "doc_total": len(docs_preview),
                "snapshots": self.snapshots.metadata(),
                "cache": self.cache.snapshot_stats(),
                "cache_report": copy.deepcopy(runtime.cache_report),
                "degradation": self._degradation_summary(result),
            }

    def inject_query_fault(
        self,
        doc_id: str,
        stage: str,
        value: Any = None,
    ) -> None:
        with self._lock:
            self._query_faults[doc_id] = {"stage": stage, "value": value}

    def clear_query_faults(self) -> None:
        with self._lock:
            self._query_faults.clear()

