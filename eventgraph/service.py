from __future__ import annotations

import shutil
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .cache import ArtifactCache
from .config import AppConfig
from .engine import IncrementalEngine
from .errors import BusinessError, fail
from .generator import DocumentGenerator
from .parser import DocumentParser
from .similarity import SimilarityScorer
from .snapshots import SnapshotStore
from .utils import (
    atomic_write_json,
    event_sort_tuple,
    pair_key,
    read_json,
    round_score,
)


class WorkbenchService:
    def __init__(
        self,
        root: str | Path,
        *,
        clear_runtime: bool = True,
        initial_docs: tuple[str, ...] = ("D01", "D02", "D03"),
    ) -> None:
        self.root = Path(root)
        self.runtime = self.root / "runtime"
        self._stage_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._error_seq = 0
        self._errors: list[dict[str, Any]] = []
        self._trust_revision = 0
        self.generator = DocumentGenerator(self.root)
        self._prepare_runtime(clear_runtime=clear_runtime)
        self.cache = ArtifactCache(self.runtime / "cache")
        self.snapshots = SnapshotStore(self.runtime / "snapshots")
        self.config = AppConfig.from_files(self.root)
        self.parser = DocumentParser(self.root, self.config.parser_version)
        self.documents: dict[str, dict[str, Any]] = {}
        self.document_order: list[str] = []
        self._load_generated_documents()
        self.engine = IncrementalEngine(
            self.root,
            self.config,
            self.parser,
            self.cache,
        )
        self._bootstrap(initial_docs)

    def _prepare_runtime(self, *, clear_runtime: bool) -> None:
        if clear_runtime and self.runtime.exists():
            shutil.rmtree(self.runtime)
        for relative in ("cache", "snapshots", "errors", "documents"):
            (self.runtime / relative).mkdir(parents=True, exist_ok=True)
        probe = self.runtime / ".write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()

    def _load_generated_documents(self) -> None:
        manifest = self.generator.generate_all()
        self.documents = {
            item["doc_id"]: read_json(item["path"]) for item in manifest
        }
        self.document_order = [item["doc_id"] for item in manifest]

    def _bootstrap(self, initial_docs: tuple[str, ...]) -> None:
        for doc_id in initial_docs:
            if doc_id in self.engine.documents:
                continue
            document = self.documents[doc_id]
            self._commit_one_locked(document)

    @contextmanager
    def _stage_guard(self) -> Iterator[None]:
        if not self._stage_lock.acquire(blocking=False):
            fail(
                "E_STAGE_BUSY",
                "已有增量提交正在生效，后到提交立即返回",
            )
        try:
            yield
        finally:
            self._stage_lock.release()

    def note_error(
        self,
        error: BusinessError,
        *,
        source: str = "api",
    ) -> dict[str, Any]:
        recorded = getattr(error, "_workbench_error_entry", None)
        if recorded is not None:
            return recorded
        with self._state_lock:
            self._error_seq += 1
            entry = {
                "sequence": self._error_seq,
                "code": error.code,
                "message": error.message,
                "details": error.details,
                "source": source,
                "recorded_at": datetime.now(timezone.utc).isoformat(),
            }
            self._errors.append(entry)
            self._errors = self._errors[-20:]
            atomic_write_json(
                self.runtime / "errors" / "recent.json",
                {"last_error_code": error.code, "errors": self._errors},
            )
            error._workbench_error_entry = entry
            return entry

    def _reset_locked(self, initial_docs: tuple[str, ...]) -> None:
        self.cache.reset()
        self.snapshots.reset()
        self._errors = []
        self._error_seq = 0
        self._trust_revision = 0
        error_path = self.runtime / "errors" / "recent.json"
        if error_path.exists():
            error_path.unlink()
        document_runtime = self.runtime / "documents"
        if document_runtime.exists():
            shutil.rmtree(document_runtime)
        document_runtime.mkdir(parents=True, exist_ok=True)
        self.config = AppConfig.from_files(self.root)
        self.parser = DocumentParser(self.root, self.config.parser_version)
        self._load_generated_documents()
        self.engine = IncrementalEngine(
            self.root,
            self.config,
            self.parser,
            self.cache,
        )
        self._bootstrap(initial_docs)

    def reset(
        self,
        *,
        initial_docs: tuple[str, ...] = ("D01", "D02", "D03"),
    ) -> dict[str, Any]:
        try:
            with self._stage_guard():
                with self._state_lock:
                    self._reset_locked(initial_docs)
            return {"ok": True, "state": self.state_view()}
        except BusinessError as error:
            self.note_error(error)
            raise

    def _commit_one_locked(
        self,
        document: dict[str, Any],
        *,
        resource_override: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        working = self.engine.clone()
        report = working.commit_document(
            document,
            resource_override=resource_override,
        )
        working.diagnose()
        snapshot_status = self._persist_snapshots(working, self.config)
        with self._state_lock:
            self.engine = working
        report = {
            **report,
            "snapshots": snapshot_status,
            "self_check": working.last_self_check,
        }
        return report

    def commit_next(
        self,
        *,
        resource_override: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            with self._stage_guard():
                processed = set(self.engine.document_order)
                next_doc = next(
                    (
                        doc_id
                        for doc_id in self.document_order
                        if doc_id not in processed
                    ),
                    None,
                )
                if next_doc is None:
                    fail(
                        "E_RESOURCE_LIMIT",
                        "没有待处理的本地文档",
                        resource="pending_documents",
                        limit=len(self.document_order),
                        actual=0,
                    )
                report = self._commit_one_locked(
                    self.documents[next_doc],
                    resource_override=resource_override,
                )
                return {"ok": True, "report": report, "state": self.state_view()}
        except BusinessError as error:
            self.note_error(error, source="commit_next")
            raise

    def _ensure_processed(self, doc_id: str) -> None:
        for candidate_id in self.document_order:
            if candidate_id in self.engine.documents:
                if candidate_id == doc_id:
                    return
                continue
            self._commit_one_locked(self.documents[candidate_id])
            if candidate_id == doc_id:
                return

    def demo_conflict(self) -> dict[str, Any]:
        try:
            with self._stage_guard():
                self._ensure_processed("D04")
                report = next(
                    (
                        item
                        for item in self.engine.stage_reports
                        if item["document_id"] == "D04"
                    ),
                    None,
                )
                records = [
                    record
                    for record in self.engine.arbitration_records
                    if record["kind"] == "chain_conflict"
                    and record["event_uid"] == "D04:E01"
                ]
                return {
                    "ok": True,
                    "report": report,
                    "conflict_records": records,
                    "state": self.state_view(),
                }
        except BusinessError as error:
            self.note_error(error, source="demo_conflict")
            raise

    def demo_degrade(self) -> dict[str, Any]:
        try:
            with self._stage_guard():
                self._ensure_processed("D06")
                if "D07" not in self.engine.documents:
                    report = self._commit_one_locked(
                        self.documents["D07"],
                        resource_override={
                            "max_candidate_comparisons": 3,
                            "degrade_floor": self.config.degrade_floor,
                            "allow_degrade": True,
                        },
                    )
                    existing = False
                else:
                    report = next(
                        (
                            item
                            for item in self.engine.stage_reports
                            if item["document_id"] == "D07"
                        ),
                        None,
                    )
                    existing = True
                return {
                    "ok": True,
                    "already_committed": existing,
                    "report": report,
                    "state": self.state_view(),
                }
        except BusinessError as error:
            self.note_error(error, source="demo_degrade")
            raise

    def invalidate(
        self,
        *,
        threshold: float | None = None,
        trust_updates: dict[str, float] | None = None,
        revise_document_id: str | None = None,
        reason: str = "manual_invalidation",
    ) -> dict[str, Any]:
        try:
            with self._stage_guard():
                old_config = self.config
                old_engine = self.engine
                new_config = old_config
                trust_changed = False
                if threshold is not None:
                    new_config = new_config.clone_with(
                        similarity_floor=float(threshold)
                    )
                if trust_updates:
                    sources = {
                        key: dict(value)
                        for key, value in new_config.trust_sources.items()
                    }
                    for source, trust in trust_updates.items():
                        if source not in sources:
                            fail(
                                "E_CONFIG_INVALID",
                                f"可信度表不存在来源 {source}",
                                field=f"trust_sources.{source}",
                            )
                        if not isinstance(trust, (int, float)) or not 0 <= float(
                            trust
                        ) <= 1:
                            fail(
                                "E_CONFIG_INVALID",
                                "来源可信度必须位于 [0, 1]",
                                field=f"trust_sources.{source}.trust",
                            )
                        if round_score(float(trust)) != round_score(
                            float(sources[source]["trust"])
                        ):
                            trust_changed = True
                        sources[source]["trust"] = round_score(float(trust))
                    if trust_changed:
                        self._trust_revision += 1
                        new_config = new_config.clone_with(
                            trust_sources=sources,
                            trust_version=(
                                f"trust-2026.11.23."
                                f"r{self._trust_revision}"
                            ),
                        )
                revised_document = None
                if revise_document_id is not None:
                    if revise_document_id not in self.documents:
                        fail(
                            "E_CONFIG_INVALID",
                            "待修订文档不存在",
                            field="revise_document_id",
                        )
                    current = self.documents[revise_document_id]
                    event_spec = current["sentences"][0]["event_specs"][0]
                    next_template = (
                        int(event_spec["template_index"]) + 1
                    ) % len(
                        self.generator.vocab["templates"][
                            event_spec["event_type"]
                        ]
                    )
                    revised_document = self.generator.revise_document_template(
                        current,
                        local_id=event_spec["local_id"],
                        template_index=next_template,
                    )
                    revised_document["sentences"][0]["event_specs"][0][
                        "template_index"
                    ] = next_template
                processed_order = list(old_engine.document_order)
                invalidation = self._calculate_invalidation_start(
                    old_engine,
                    new_config,
                    trust_updates=trust_updates,
                    trust_changed=trust_changed,
                    revise_document_id=revise_document_id,
                )
                start_index = invalidation["start_index"]
                if revised_document is not None:
                    self.documents[revise_document_id] = revised_document
                    atomic_write_json(
                        self.runtime
                        / "documents"
                        / f"{revise_document_id}.json",
                        revised_document,
                    )
                new_engine = self._build_recomputed_engine(
                    old_engine,
                    old_config,
                    new_config,
                    processed_order,
                    start_index,
                )
                new_engine.diagnose()
                merged_checkpoints = {
                    doc_id: state
                    for doc_id, state in old_engine.checkpoints.items()
                    if doc_id in processed_order[:start_index]
                }
                merged_checkpoints.update(new_engine.checkpoints)
                new_engine.checkpoints = merged_checkpoints
                snapshot_status = self._persist_snapshots(
                    new_engine,
                    new_config,
                )
                recomputed_doc_ids = processed_order[start_index:]
                recalculated_chain_ids = []
                for chain_id, chain in new_engine.chains.items():
                    if any(
                        new_engine.events[event_uid]["document_id"]
                        in recomputed_doc_ids
                        for event_uid in chain["members"]
                    ):
                        recalculated_chain_ids.append(chain_id)
                reused_chain_ids = sorted(
                    set(new_engine.chains) - set(recalculated_chain_ids)
                )
                result = {
                    "reason": reason,
                    "start_document": (
                        processed_order[start_index]
                        if start_index < len(processed_order)
                        else None
                    ),
                    "recomputed_document_ids": recomputed_doc_ids,
                    "reused_document_ids": processed_order[:start_index],
                    "affected_candidate_pairs": invalidation[
                        "affected_pairs"
                    ],
                    "affected_chain_ids": invalidation[
                        "affected_chain_ids"
                    ],
                    "recalculated_chain_ids": sorted(recalculated_chain_ids),
                    "reused_chain_ids": reused_chain_ids,
                    "threshold_before": old_config.similarity_floor,
                    "threshold_after": new_config.similarity_floor,
                    "trust_version_before": old_config.trust_version,
                    "trust_version_after": new_config.trust_version,
                    "revised_document_id": revise_document_id,
                    "cache": self.cache.stats(),
                    "self_check": new_engine.last_self_check,
                    "snapshots": snapshot_status,
                }
                new_engine.last_invalidation = result
                with self._state_lock:
                    self.config = new_config
                    self.engine = new_engine
                return {"ok": True, "invalidation": result, "state": self.state_view()}
        except BusinessError as error:
            self.note_error(error, source="invalidate")
            raise

    def _calculate_invalidation_start(
        self,
        engine: IncrementalEngine,
        new_config: AppConfig,
        *,
        trust_updates: dict[str, float] | None,
        trust_changed: bool,
        revise_document_id: str | None,
    ) -> dict[str, Any]:
        processed_order = list(engine.document_order)
        if not processed_order:
            return {"start_index": 0, "affected_pairs": [], "affected_chain_ids": []}
        start_index = len(processed_order)
        affected_pairs = []
        affected_events: set[str] = set()
        if revise_document_id in processed_order:
            start_index = min(
                start_index,
                processed_order.index(revise_document_id),
            )
            revised_events = set(
                engine.documents[revise_document_id]["event_uids"]
            )
            affected_events.update(revised_events)
            for pair_id, score in engine.pair_evaluations.items():
                pair_events = set(pair_id.split("|"))
                if not pair_events & revised_events:
                    continue
                affected_pairs.append(
                    {
                        "pair_id": pair_id,
                        "left_event": pair_id.split("|")[0],
                        "right_event": pair_id.split("|")[1],
                        "old_total": score["total"],
                        "new_total": None,
                        "old_candidate": pair_id in engine.candidates,
                        "new_candidate": None,
                        "reason": "document_revised",
                    }
                )
        if trust_changed and trust_updates:
            for index, doc_id in enumerate(processed_order):
                if engine.documents[doc_id]["source"] in trust_updates:
                    start_index = min(start_index, index)
                    affected_events.update(
                        engine.documents[doc_id]["event_uids"]
                    )
        if (
            new_config.similarity_floor != engine.config.similarity_floor
            or trust_changed
        ) and revise_document_id is None:
            scorer = SimilarityScorer(self.cache, new_config)
            event_uids = sorted(
                engine.events,
                key=lambda uid: event_sort_tuple(engine.events[uid]),
            )
            old_floor = engine.config.similarity_floor
            new_floor = new_config.similarity_floor
            for left_index, left_uid in enumerate(event_uids):
                left = engine.events[left_uid]
                for right_uid in event_uids[left_index + 1 :]:
                    right = engine.events[right_uid]
                    if left["document_id"] == right["document_id"]:
                        continue
                    pair_id = pair_key(left_uid, right_uid)
                    old_score = engine.pair_evaluations.get(pair_id)
                    if old_score is None:
                        continue
                    new_score = scorer.score(
                        left,
                        right,
                        left_hash=engine.event_hashes[left_uid],
                        right_hash=engine.event_hashes[right_uid],
                        left_doc_digest=left["document_digest"],
                        right_doc_digest=right["document_digest"],
                    )
                    old_candidate = float(old_score["total"]) >= old_floor
                    new_candidate = float(new_score["total"]) >= new_floor
                    materially_changed = (
                        old_candidate != new_candidate
                        or (
                            old_candidate
                            and new_candidate
                            and round_score(float(old_score["total"]))
                            != round_score(float(new_score["total"]))
                        )
                    )
                    if not materially_changed:
                        continue
                    pair = {
                        "pair_id": pair_id,
                        "left_event": left_uid,
                        "right_event": right_uid,
                        "old_total": old_score["total"],
                        "new_total": new_score["total"],
                        "old_candidate": old_candidate,
                        "new_candidate": new_candidate,
                    }
                    affected_pairs.append(pair)
                    affected_events.update((left_uid, right_uid))
                    left_doc = engine.events[left_uid]["document_id"]
                    right_doc = engine.events[right_uid]["document_id"]
                    start_index = min(
                        start_index,
                        max(
                            processed_order.index(left_doc),
                            processed_order.index(right_doc),
                        ),
                    )
        affected_chain_ids = sorted(
            {
                engine.membership[event_uid]
                for event_uid in affected_events
                if event_uid in engine.membership
            }
        )
        return {
            "start_index": start_index,
            "affected_pairs": affected_pairs,
            "affected_chain_ids": affected_chain_ids,
        }

    def _build_recomputed_engine(
        self,
        old_engine: IncrementalEngine,
        old_config: AppConfig,
        new_config: AppConfig,
        processed_order: list[str],
        start_index: int,
    ) -> IncrementalEngine:
        if start_index <= 0:
            new_engine = IncrementalEngine(
                self.root,
                new_config,
                self.parser,
                self.cache,
            )
        elif start_index >= len(processed_order):
            new_engine = old_engine.clone(config=new_config)
        else:
            prefix_doc = processed_order[start_index - 1]
            prefix_state = old_engine.checkpoints.get(prefix_doc)
            if prefix_state is None:
                fail(
                    "E_INCREMENTAL_DRIFT",
                    "缺少受影响后缀之前的检查点",
                    document_id=prefix_doc,
                    chain_id="-",
                    event_id="-",
                    reason="missing_prefix_checkpoint",
                )
            new_engine = IncrementalEngine(
                self.root,
                new_config,
                self.parser,
                self.cache,
                state=prefix_state,
            )
        for doc_id in processed_order[start_index:]:
            new_engine.commit_document(self.documents[doc_id])
        return new_engine

    def _persist_snapshots(
        self,
        engine: IncrementalEngine,
        config: AppConfig,
    ) -> dict[str, Any]:
        trust_payload = {
            "schema": "trust-table-snapshot-v1",
            "version": config.trust_version,
            "sources": config.trust_sources,
        }
        self.snapshots.write("chains", engine.public_snapshot_payload())
        self.snapshots.write("trust_table", trust_payload)
        self.snapshots.write(
            "arbitration",
            engine.public_arbitration_payload(),
        )
        return self.snapshots.status()

    def snapshot_rollback_probe(self) -> None:
        try:
            with self._stage_lock:
                self.snapshots.probe_rollback("chains")
        except BusinessError as error:
            self.note_error(error, source="snapshot_rollback_probe")
            raise

    def snapshot_corruption_probe(self) -> None:
        try:
            with self._stage_lock:
                self.snapshots.probe_corruption("arbitration")
        except BusinessError as error:
            self.note_error(error, source="snapshot_corruption_probe")
            raise

    def stage_busy_probe(self) -> None:
        started = threading.Event()
        finished = threading.Event()

        def holder() -> None:
            self._stage_lock.acquire()
            try:
                started.set()
                time.sleep(0.45)
            finally:
                self._stage_lock.release()
                finished.set()

        thread = threading.Thread(target=holder, daemon=True)
        thread.start()
        started.wait(timeout=1.0)
        try:
            self.commit_next()
        finally:
            finished.wait(timeout=2.0)
            thread.join(timeout=1.0)

    def state_view(self) -> dict[str, Any]:
        with self._state_lock:
            engine = self.engine
            config = self.config
            candidate_edges = []
            for pair_id, candidate in self._sorted_candidates(engine):
                left_chain = engine.membership.get(candidate["left_event"])
                right_chain = engine.membership.get(candidate["right_event"])
                candidate_edges.append(
                    {
                        **candidate,
                        "left_chain": left_chain,
                        "right_chain": right_chain,
                        "status": (
                            "accepted"
                            if left_chain == right_chain
                            else "cross_chain_conflict"
                        ),
                    }
                )
            snapshot_status = self.snapshots.status()
            docs = []
            for doc_id in self.document_order:
                document = self.documents[doc_id]
                source = document["source"]
                processed = doc_id in engine.documents
                docs.append(
                    {
                        "doc_id": doc_id,
                        "doc_index": document["doc_index"],
                        "source": source,
                        "source_label": config.trust_sources.get(
                            source,
                            {"label": source},
                        )["label"],
                        "trust": config.trust.get(source),
                        "content": document["content"],
                        "processed": processed,
                        "event_count": len(
                            document["sentences"][0]["event_specs"]
                        ),
                        "event_uids": [
                            f"{doc_id}:{event['local_id']}"
                            for event in document["sentences"][0]["event_specs"]
                        ],
                    }
                )
            last_error = self._errors[-1] if self._errors else None
            return {
                "ok": True,
                "app": {
                    "title": "跨文档事件共指链诊断工作台",
                    "mode": "local-deterministic",
                    "seed": self.generator.seed,
                    "runtime_path": str(self.runtime),
                    "write_probe": "ok",
                },
                "config": config.as_public_dict(),
                "documents": docs,
                "chains": engine.chain_view(),
                "candidate_edges": candidate_edges,
                "arbitration_records": engine.arbitration_records,
                "last_stage": (
                    engine.stage_reports[-1] if engine.stage_reports else None
                ),
                "stages": engine.stage_reports[-10:],
                "last_self_check": engine.last_self_check,
                "last_invalidation": engine.last_invalidation,
                "cache": self.cache.stats(),
                "snapshots": snapshot_status,
                "resources": {
                    "max_doc_events": config.max_doc_events,
                    "max_candidate_comparisons": (
                        config.max_candidate_comparisons
                    ),
                    "max_chain_memory_bytes": config.max_chain_memory_bytes,
                    "estimated_chain_memory_bytes": (
                        engine.chain_memory_estimate()
                    ),
                    "candidate_count": len(engine.candidates),
                    "pair_evaluation_count": len(engine.pair_evaluations),
                },
                "recent_errors": list(reversed(self._errors[-8:])),
                "last_error_code": (
                    last_error["code"] if last_error else None
                ),
                "counter": {
                    "chains": len(engine.chains),
                    "events": len(engine.events),
                    "documents_processed": len(engine.documents),
                    "documents_total": len(self.document_order),
                    "arbitration_records": len(
                        engine.arbitration_records
                    ),
                    "chain_conflicts": sum(
                        1
                        for record in engine.arbitration_records
                        if record["kind"] == "chain_conflict"
                    ),
                    "argument_conflicts": sum(
                        1
                        for record in engine.arbitration_records
                        if record["kind"] == "argument_conflict"
                    ),
                },
            }

    @staticmethod
    def _sorted_candidates(
        engine: IncrementalEngine,
    ) -> list[tuple[str, dict[str, Any]]]:
        return sorted(
            engine.candidates.items(),
            key=lambda item: (
                -float(item[1]["total"]),
                item[1]["left_document"],
                item[1]["left_event"],
                item[1]["right_document"],
                item[1]["right_event"],
            ),
        )
