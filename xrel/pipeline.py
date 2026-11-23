"""End-to-end pipeline facade for the cross-lingual relevance application."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .canonical import canonical_bytes, content_checksum
from .config import AppConfig, default_config
from .concurrency import ChangeSet, InvalidationPlan, StageLock, plan_invalidation
from .error_spectrum import build_spectrum
from .errors import LineageBroken
from .expansion import ExpansionResult, expand_query
from .index import InvertedIndex, build_index
from .lineage import (
    LineageRecord,
    ResultStore,
    build_cache_key,
    check_freshness,
    result_fingerprint,
    verify_lineage,
)
from .model import BridgeResource, Document, Query
from .numerics import make_backend
from .scoring import DocScore, ScoreEngine, build_doc_lengths
from .snapshot import SnapshotRef, SnapshotStore
from .topk import select_topk


NODE_PATH = ["expand", "score", "partition_reduce", "topk", "error_spectrum"]


@dataclass
class QueryArtifact:
    """Full per-query, per-precision artifact used for reuse and replay."""

    query_id: str
    precision: str
    expansion: ExpansionResult
    doc_scores: Dict[str, DocScore]
    topk: Any
    spectrum: Any
    budget: Dict[str, Any]
    cache_key: str


class XrelPipeline:
    def __init__(
        self,
        snapshot_dir: Path,
        result_dir: Path,
        config: Optional[AppConfig] = None,
    ) -> None:
        self.snapshot_store = SnapshotStore(Path(snapshot_dir))
        self.result_store = ResultStore(Path(result_dir))
        self.config = config or default_config()
        self.config.validate()
        self.commit_lock = StageLock("resource_commit")
        # In-memory deterministic artifact caches keyed per precision.
        self._expansion_cache: Dict[str, ExpansionResult] = {}
        self._artifact_cache: Dict[Tuple[str, str], QueryArtifact] = {}
        self.documents: List[Document] = []
        self.queries: List[Query] = []
        self.bridge: Optional[BridgeResource] = None
        self.index: Optional[InvertedIndex] = None

    # ------------------------------------------------------------------
    # Loading / snapshots
    # ------------------------------------------------------------------
    def load_inputs(
        self,
        documents: List[Document],
        queries: List[Query],
        bridge: BridgeResource,
        publish: bool = True,
    ) -> None:
        self.documents = sorted(documents, key=lambda doc: doc.doc_id)
        self.queries = sorted(queries, key=lambda query: query.query_id)
        self.bridge = bridge
        self.index = build_index(self.documents, version=bridge.version)
        if publish:
            with self.commit_lock:
                self.snapshot_store.publish_directions(bridge.version, bridge.directions)
                self.snapshot_store.publish_bridge(bridge)
                self.snapshot_store.publish_index(self.index)
        self.invalidate_caches()

    def invalidate_caches(self) -> None:
        self._expansion_cache.clear()
        self._artifact_cache.clear()

    # ------------------------------------------------------------------
    def _refs(self) -> Tuple[SnapshotRef, SnapshotRef]:
        bridge_ref = self.snapshot_store.current(SnapshotStore.BRIDGE)
        index_ref = self.snapshot_store.current(SnapshotStore.INDEX)
        if bridge_ref is None or index_ref is None:
            raise LineageBroken("-", "snapshot refs missing before pipeline run")
        return bridge_ref, index_ref

    def _expansion_for(self, query: Query) -> ExpansionResult:
        cache_key = f"{query.query_id}:v{self.bridge.version}:{self.bridge.checksum}"
        if cache_key not in self._expansion_cache:
            self._expansion_cache[cache_key] = expand_query(
                query, self.bridge, self.config.expand
            )
        return self._expansion_cache[cache_key]

    # ------------------------------------------------------------------
    def run_query(self, query: Query, precision: Optional[str] = None) -> QueryArtifact:
        precision = precision or self.config.precision
        config = self.config if precision == self.config.precision else self.config.with_precision(
            precision, self.config.fixed
        )
        bridge_ref, index_ref = self._refs()
        cache_key = build_cache_key(
            query.query_id, precision, bridge_ref, index_ref, config
        )
        cached = self._artifact_cache.get((query.query_id, precision))
        if cached is not None and cached.cache_key == cache_key:
            return cached

        expansion = self._expansion_for(query)
        backend = make_backend(precision, config.fixed)
        engine = ScoreEngine(backend, config)
        doc_lengths = build_doc_lengths(self.documents)
        doc_scores = engine.score(expansion, self.index, doc_lengths)
        topk = select_topk(
            query.query_id, doc_scores, query.top_k, config.threshold, backend
        )
        artifact = QueryArtifact(
            query_id=query.query_id,
            precision=precision,
            expansion=expansion,
            doc_scores=doc_scores,
            topk=topk,
            spectrum=None,
            budget=engine.counters.to_dict(),
            cache_key=cache_key,
        )
        self._artifact_cache[(query.query_id, precision)] = artifact
        return artifact

    # ------------------------------------------------------------------
    def run_with_spectrum(
        self, query: Query, precision: str
    ) -> QueryArtifact:
        reference = self.run_query(query, "float64")
        artifact = self.run_query(query, precision)
        if precision != "float64":
            backend = make_backend(precision, self.config.fixed)
            artifact.spectrum = build_spectrum(
                precision,
                reference.doc_scores,
                artifact.doc_scores,
                backend,
            )
        else:
            artifact.spectrum = build_spectrum(
                "float64", reference.doc_scores, reference.doc_scores, make_backend("float64")
            )
        return artifact

    # ------------------------------------------------------------------
    def build_result_record(
        self, query: Query, precision: str, artifact: QueryArtifact
    ) -> LineageRecord:
        bridge_ref, index_ref = self._refs()
        result = {
            "query_id": query.query_id,
            "precision": precision,
            "expansion": artifact.expansion.to_dict(),
            "scores": {
                doc_id: doc_score.to_dict()
                for doc_id, doc_score in sorted(artifact.doc_scores.items())
            },
            "topk": artifact.topk.to_dict(),
            "error_spectrum": None if artifact.spectrum is None else artifact.spectrum.to_dict(),
            "budget": artifact.budget,
        }
        record = LineageRecord(
            query_id=query.query_id,
            cache_key=artifact.cache_key,
            precision=precision,
            bridge_version=bridge_ref.version,
            bridge_checksum=bridge_ref.checksum,
            index_version=index_ref.version,
            index_checksum=index_ref.checksum,
            node_path=list(NODE_PATH),
            result_checksum=result_fingerprint(result),
            result=result,
        )
        return record

    # ------------------------------------------------------------------
    # Query-side API with stale rejection and replay
    # ------------------------------------------------------------------
    def query_by_id(
        self, query_id: str, precision: Optional[str] = None, use_cache: bool = True
    ) -> LineageRecord:
        precision = precision or self.config.precision
        query = self._require_query(query_id)
        bridge_ref, index_ref = self._refs()
        config = self.config if precision == self.config.precision else self.config.with_precision(
            precision, self.config.fixed
        )
        cache_key = build_cache_key(query_id, precision, bridge_ref, index_ref, config)

        if use_cache:
            cached = self.result_store.load(cache_key)
            if cached is not None:
                check_freshness(cached, self.snapshot_store)
                verify_lineage(cached)
                self._assert_replay(query, cached, precision)
                return cached

        artifact = self.run_with_spectrum(query, precision)
        record = self.build_result_record(query, precision, artifact)
        verify_lineage(record)
        self.result_store.save(record)
        return record

    def _assert_replay(self, query: Query, record: LineageRecord, precision: str) -> None:
        replayed = self.query_by_id(query.query_id, precision, use_cache=False)
        if canonical_bytes(replayed.result) != canonical_bytes(record.result):
            raise LineageBroken(
                query.query_id,
                "replay produced a different result than the stored lineage record",
            )

    # ------------------------------------------------------------------
    # Batch side and cross-module self check
    # ------------------------------------------------------------------
    def run_batch(self, precision: Optional[str] = None) -> Dict[str, LineageRecord]:
        precision = precision or self.config.precision
        records: Dict[str, LineageRecord] = {}
        for query in self.queries:
            artifact = self.run_with_spectrum(query, precision)
            record = self.build_result_record(query, precision, artifact)
            verify_lineage(record)
            self.result_store.save(record)
            records[query.query_id] = record
        return records

    def self_check_query_vs_batch(self, precision: Optional[str] = None) -> Dict[str, Any]:
        precision = precision or self.config.precision
        batch = self.run_batch(precision)
        mismatches = []
        for query in self.queries:
            query_side = self.query_by_id(query.query_id, precision, use_cache=False)
            if canonical_bytes(query_side.result) != canonical_bytes(batch[query.query_id].result):
                mismatches.append(query.query_id)
        return {
            "checked_queries": len(self.queries),
            "mismatches": mismatches,
            "consistent": not mismatches,
        }

    # ------------------------------------------------------------------
    # Incremental commit with suffix recomputation and reuse
    # ------------------------------------------------------------------
    def _artifact_content(self, query: Query, artifact: QueryArtifact) -> Dict[str, Any]:
        return {
            "query_id": query.query_id,
            "precision": artifact.precision,
            "expansion": artifact.expansion.to_dict(),
            "scores": {
                doc_id: doc_score.to_dict()
                for doc_id, doc_score in sorted(artifact.doc_scores.items())
            },
            "topk": artifact.topk.to_dict(),
            "error_spectrum": None
            if artifact.spectrum is None
            else artifact.spectrum.to_dict(),
            "budget": artifact.budget,
        }

    def _content_fingerprint(self, query: Query, artifact: QueryArtifact) -> str:
        return content_checksum(self._artifact_content(query, artifact))

    def commit_increment(
        self,
        change: ChangeSet,
        new_bridge: Optional[BridgeResource] = None,
        new_documents: Optional[List[Document]] = None,
    ) -> Dict[str, Any]:
        with self.commit_lock:
            cached_ids = {query.query_id for query in self.queries}
            plan = plan_invalidation(change, self.bridge, self.queries, cached_ids)

            # Snapshot reusable (unaffected) artifacts before replacement.
            reusable: Dict[Tuple[str, str], QueryArtifact] = {
                key: value
                for key, value in self._artifact_cache.items()
                if key[0] in set(plan.reuse_queries)
            }

            if new_bridge is not None:
                self.bridge = new_bridge
                self.snapshot_store.publish_directions(new_bridge.version, new_bridge.directions)
                self.snapshot_store.publish_bridge(new_bridge)
            if new_documents is not None:
                merged = {doc.doc_id: doc for doc in self.documents}
                for doc in new_documents:
                    merged[doc.doc_id] = doc
                self.documents = sorted(merged.values(), key=lambda item: item.doc_id)

            self.index = build_index(self.documents, version=self._next_index_version())
            self.snapshot_store.publish_index(self.index)

            # Whole cache invalidates, then unaffected upstream artifacts restored.
            self.invalidate_caches()
            self._artifact_cache.update(reusable)

            # Incremental pass: recompute affected suffixes, reuse the rest,
            # persisting fresh lineage records under the new cache keys.
            incremental_fingerprints = self._incremental_pass(plan)

            # Independent full recompute bypassing every cached artifact.
            self.invalidate_caches()
            full_fingerprints = self._fresh_full_fingerprints()

            verification = self._verify_incremental_consistency(
                plan, incremental_fingerprints, full_fingerprints
            )
            return {
                "plan": plan.to_dict(),
                "recomputed_queries": plan.recompute_queries,
                "reused_queries": plan.reuse_queries,
                "verification": verification,
            }

    def _incremental_pass(self, plan: InvalidationPlan) -> Dict[str, Dict[str, str]]:
        fingerprints: Dict[str, Dict[str, str]] = {}
        for query in self.queries:
            tiers: Dict[str, str] = {}
            for precision in ("float64", "float32", "fixed"):
                artifact = self.run_with_spectrum(query, precision)
                record = self.build_result_record(query, precision, artifact)
                self.result_store.save(record)
                tiers[precision] = self._content_fingerprint(query, artifact)
            fingerprints[query.query_id] = tiers
        return fingerprints

    def _fresh_full_fingerprints(self) -> Dict[str, Dict[str, str]]:
        fingerprints: Dict[str, Dict[str, str]] = {}
        for query in self.queries:
            tiers: Dict[str, str] = {}
            for precision in ("float64", "float32", "fixed"):
                artifact = self.run_with_spectrum(query, precision)
                tiers[precision] = self._content_fingerprint(query, artifact)
            fingerprints[query.query_id] = tiers
        return fingerprints

    def run_batch_for_query(self, query_id: str) -> Dict[str, str]:
        query = self._require_query(query_id)
        for precision in ("float64", "float32", "fixed"):
            artifact = self.run_with_spectrum(query, precision)
            record = self.build_result_record(query, precision, artifact)
            self.result_store.save(record)
        return {
            precision: self._content_fingerprint(query, self._artifact_cache[(query_id, precision)])
            for precision in ("float64", "float32", "fixed")
        }

    def _verify_incremental_consistency(
        self,
        plan: InvalidationPlan,
        incremental: Dict[str, Dict[str, str]],
        full_fingerprints: Dict[str, Dict[str, str]],
    ) -> Dict[str, Any]:
        recompute_set = set(plan.recompute_queries)
        reuse_set = set(plan.reuse_queries)
        affected = set(plan.affected_queries)
        all_ids = {q.query_id for q in self.queries}
        partition_ok = recompute_set.isdisjoint(reuse_set)
        coverage_ok = (recompute_set | reuse_set) == all_ids

        byte_ok = True
        mismatches = []
        for query_id, tiers in full_fingerprints.items():
            for precision, checksum in tiers.items():
                if incremental[query_id][precision] != checksum:
                    byte_ok = False
                    mismatches.append({"query_id": query_id, "precision": precision})

        return {
            "recompute_reuse_disjoint": partition_ok,
            "union_covers_all": coverage_ok,
            "affected_covered": affected <= (recompute_set | reuse_set),
            "byte_for_byte_equal_to_full_recompute": byte_ok,
            "mismatches": mismatches,
        }

    # ------------------------------------------------------------------
    def _require_query(self, query_id: str) -> Query:
        for query in self.queries:
            if query.query_id == query_id:
                return query
        raise KeyError(f"unknown query_id: {query_id}")

    def _next_index_version(self) -> int:
        current = self.snapshot_store.current(SnapshotStore.INDEX)
        base = current.version if current is not None else 0
        return max(base + 1, self.bridge.version)
