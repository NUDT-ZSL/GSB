"""End-to-end four-stage engine with deterministic recovery semantics."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from .analysis import (
    AnalyzedDoc,
    analyze_jsonl_files,
)
from .checkpoint import Checkpoint, CheckpointStore, PHASES
from .compression import (
    CompressionCache,
    CompressionParams,
    refresh_compression_cache,
)
from .config import (
    ConfigLoader,
    NormalizedConfig,
    normalize_term,
)
from .errors import (
    DiagnosticError,
    E_BAD_DOC,
    E_CONFIG_ROLLBACK,
    E_FAULT_INJECTED,
    E_IO,
    E_SNAPSHOT_CORRUPT,
    E_SNAPSHOT_ROLLBACK,
    E_TERM_NOT_FOUND,
    E_VERSION_UNSUPPORTED,
)
from .ledger import CodeJournal, LedgerManager
from .postings import (
    PostingMap,
    append_documents,
    compute_term_statistics,
    deserialize_postings,
    estimate_postings_bytes,
    postings_digest,
    postings_to_sequence,
    rebuild_terms,
    serialize_postings,
    sort_documents,
    term_postings_hash,
    validate_postings_against_documents,
)
from .resources import ResourceGuard
from .util import (
    atomic_write_json,
    read_json,
    sha256_json,
)


class InjectedFault(RuntimeError):
    code = E_FAULT_INJECTED

    def __init__(self, phase: str, when: str):
        super().__init__(f"injected fault at {phase}:{when}")
        self.phase = phase
        self.when = when


@dataclass
class _FaultPlan:
    phase: str
    when: str


class IndexEngine:
    """A local index build, migration, and recovery coordinator."""

    def __init__(
        self,
        work_dir: str | os.PathLike[str],
        *,
        config_path: str | os.PathLike[str] | None = None,
    ):
        self.work_dir = Path(work_dir)
        self.state_dir = self.work_dir / "state"
        self.cache_dir = self.work_dir / "cache"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.checkpoint_store = CheckpointStore(self.state_dir / "checkpoint.json")
        self.cache_path = self.cache_dir / "compression.json"
        self.invalidation_path = self.state_dir / "last_invalidation.json"
        self.code_journal = CodeJournal(self.state_dir / "last_code.json")
        self.ledger_manager = LedgerManager(
            self.state_dir / "ledger.json",
            self.state_dir / "config_snapshot.json",
            lock_path=self.state_dir / "commit.lock",
        )
        self.config: NormalizedConfig | None = None
        self.checkpoint: Checkpoint | None = None
        self.documents: list[AnalyzedDoc] = []
        self.postings: PostingMap = {}
        self.posting_statistics: dict[str, dict[str, Any]] = {}
        self.cache: CompressionCache | None = None
        self.batch_info: list[dict[str, Any]] = []
        self.metrics: dict[str, Any] = {}
        self.resource_actions: list[dict[str, Any]] = []
        self.last_invalidation: dict[str, Any] = {}
        self.compression_config_hash: str | None = None
        self.ledger_config_hash: str | None = None
        self.ledger_version: int = 0
        self.pending_replacement: dict[str, Any] | None = None
        self._adopt_checkpoint(self.checkpoint_store.load())
        if config_path is not None:
            self.configure(config_path)

    @property
    def completed(self) -> dict[str, set[int]]:
        if self.checkpoint is None:
            return {phase: set() for phase in PHASES}
        return {
            phase: set(self.checkpoint.completed_by_phase.get(phase, []))
            for phase in PHASES
        }

    def _adopt_checkpoint(self, checkpoint: Checkpoint | None) -> None:
        self.checkpoint = checkpoint
        self.documents = []
        self.postings = {}
        self.posting_statistics = {}
        self.cache = None
        self.batch_info = []
        self.metrics = {}
        self.resource_actions = []
        self.last_invalidation = {}
        self.compression_config_hash = None
        self.ledger_config_hash = None
        self.ledger_version = 0
        self.pending_replacement = None
        if checkpoint is None:
            return
        payload = checkpoint.payload
        try:
            stored_config = payload["config"]
            config = NormalizedConfig.from_dict(stored_config)
            if stored_config.get("config_hash") not in (None, config.config_hash):
                raise ValueError("stored configuration hash mismatch")
            documents = [
                AnalyzedDoc.from_dict(value) for value in payload.get("documents", [])
            ]
            document_ids = [document.doc_id for document in documents]
            if len(document_ids) != len(set(document_ids)):
                raise ValueError("checkpoint contains duplicate doc_id")
            postings = deserialize_postings(payload.get("postings", {}))
            cache_value = payload.get("compression")
            cache = (
                CompressionCache.from_dict(cache_value)
                if cache_value is not None
                else None
            )
            batch_info = payload.get("batch_info")
            metrics = payload.get("metrics", {})
            resource_actions = payload.get("resource_actions", [])
            posting_statistics = payload.get("posting_statistics", {})
            last_invalidation = payload.get("last_invalidation", {})
            pending_replacement = payload.get("pending_replacement")
            compression_hash = payload.get("compression_config_hash")
            ledger_hash = payload.get("ledger_config_hash")
            ledger_version = int(payload.get("ledger_version", 0))
            if not isinstance(metrics, dict):
                raise ValueError("metrics must be an object")
            if not isinstance(resource_actions, list):
                raise ValueError("resource_actions must be an array")
            if not isinstance(posting_statistics, dict):
                raise ValueError("posting_statistics must be an object")
            if not isinstance(last_invalidation, dict):
                raise ValueError("last_invalidation must be an object")
            if pending_replacement is not None and not isinstance(
                pending_replacement, dict
            ):
                raise ValueError("pending_replacement must be null or an object")
        except DiagnosticError:
            raise
        except (TypeError, ValueError, KeyError) as exc:
            raise DiagnosticError(
                E_SNAPSHOT_CORRUPT,
                "checkpoint payload is invalid",
                {"checkpoint_version": checkpoint.version, "error": str(exc)},
            ) from exc

        self.config = config
        self.documents = sort_documents(documents)
        self.postings = postings
        self.posting_statistics = posting_statistics
        self.cache = cache
        self.batch_info = (
            batch_info if isinstance(batch_info, list) else self._build_batch_info()
        )
        self.metrics = metrics
        self.resource_actions = resource_actions
        self.last_invalidation = last_invalidation
        self.pending_replacement = pending_replacement
        self.compression_config_hash = (
            compression_hash
            if isinstance(compression_hash, str)
            else (
                config.config_hash
                if cache is not None
                and set(checkpoint.completed_by_phase.get("compress", []))
                else None
            )
        )
        self.ledger_config_hash = (
            ledger_hash
            if isinstance(ledger_hash, str)
            else (
                config.config_hash
                if set(checkpoint.completed_by_phase.get("ledger", []))
                else None
            )
        )
        self.ledger_version = ledger_version

    def _reload_checkpoint_state(self) -> None:
        self._adopt_checkpoint(self.checkpoint_store.load())

    def _safe_reload_checkpoint_state(self) -> None:
        try:
            self._reload_checkpoint_state()
        except DiagnosticError:
            pass

    def _build_batch_info(self) -> list[dict[str, Any]]:
        groups: dict[int, list[AnalyzedDoc]] = {}
        for document in self.documents:
            groups.setdefault(document.batch_no, []).append(document)
        result = []
        for batch_no in sorted(groups):
            docs = sort_documents(groups[batch_no])
            result.append(
                {
                    "batch_no": batch_no,
                    "doc_ids": [doc.doc_id for doc in docs],
                    "document_digest": sha256_json(
                        [doc.canonical_summary() for doc in docs]
                    ),
                    "terms": sorted(
                        {
                            term
                            for doc in docs
                            for term, _ in doc.term_frequencies
                        }
                    ),
                }
            )
        return result

    def _checkpoint_payload(self) -> dict[str, Any]:
        if self.config is None:
            raise DiagnosticError(
                E_VERSION_UNSUPPORTED,
                "no configuration is available for checkpoint",
            )
        return {
            "batch_info": self.batch_info,
            "compression": self.cache.to_dict() if self.cache is not None else None,
            "compression_config_hash": self.compression_config_hash,
            "config": self.config.to_dict(),
            "documents": [document.to_dict() for document in self.documents],
            "last_invalidation": self.last_invalidation,
            "ledger_config_hash": self.ledger_config_hash,
            "ledger_version": self.ledger_version,
            "metrics": self.metrics,
            "pending_replacement": self.pending_replacement,
            "posting_statistics": self.posting_statistics,
            "postings": serialize_postings(self.postings),
            "resource_actions": self.resource_actions,
        }

    def _completed_map(self) -> dict[str, list[int]]:
        return {phase: sorted(self.completed[phase]) for phase in PHASES}

    def _should_fault(self, phase: str, when: str) -> bool:
        plan = getattr(self, "_fault_plan", None)
        return bool(plan and plan.phase == phase and plan.when == when)

    def _commit_phase(
        self,
        phase: str,
        expected_batches: Iterable[int],
        *,
        completed_override: Mapping[str, Iterable[int]] | None = None,
    ) -> Checkpoint:
        if self._should_fault(phase, "before_checkpoint"):
            self._reload_checkpoint_state()
            raise InjectedFault(phase, "before_checkpoint")
        completed = (
            {name: list(values) for name, values in completed_override.items()}
            if completed_override is not None
            else self._completed_map()
        )
        checkpoint = self.checkpoint_store.commit(
            phase=phase,
            completed_by_phase=completed,
            expected_batches=list(expected_batches),
            payload=self._checkpoint_payload(),
        )
        self.checkpoint = checkpoint
        if self._should_fault(phase, "after_checkpoint"):
            raise InjectedFault(phase, "after_checkpoint")
        return checkpoint

    def _load_fault_plan(
        self,
        fault_path: str | os.PathLike[str] | None,
        recovery_path: str | os.PathLike[str] | None,
    ) -> _FaultPlan | None:
        if recovery_path is not None:
            recovery_file = Path(recovery_path)
            if not recovery_file.exists():
                raise DiagnosticError(
                    E_IO,
                    "recovery instruction file does not exist",
                    {"path": str(recovery_file)},
                )
            instruction = read_json(recovery_file)
            if not isinstance(instruction, dict):
                raise DiagnosticError(
                    E_IO,
                    "recovery instruction must be an object",
                    {"path": str(recovery_file)},
                )
            action = instruction.get("action", instruction.get("command"))
            resume = instruction.get(
                "resume", instruction.get("clear_fault", action in {"resume", "recover"})
            )
            if not resume:
                raise DiagnosticError(
                    E_IO,
                    "recovery instruction does not request resume",
                    {"path": str(recovery_file)},
                )
            expected_version = instruction.get("from_checkpoint")
            if expected_version is not None:
                if (
                    isinstance(expected_version, bool)
                    or not isinstance(expected_version, int)
                    or expected_version <= 0
                ):
                    raise DiagnosticError(
                        E_IO,
                        "recovery from_checkpoint must be a positive integer",
                    )
                current = self.checkpoint.version if self.checkpoint else 0
                if expected_version > current:
                    raise DiagnosticError(
                        E_SNAPSHOT_ROLLBACK,
                        "requested recovery checkpoint is newer than local state",
                        {"requested": expected_version, "current": current},
                    )
            return None
        if fault_path is None:
            return None
        fault_file = Path(fault_path)
        if not fault_file.exists():
            raise DiagnosticError(
                E_IO,
                "fault instruction file does not exist",
                {"path": str(fault_file)},
            )
        instruction = read_json(fault_file)
        if not isinstance(instruction, dict):
            raise DiagnosticError(
                E_IO,
                "fault instruction must be an object",
                {"path": str(fault_file)},
            )
        phase = instruction.get("phase", instruction.get("fail_after_phase"))
        if phase not in PHASES:
            raise DiagnosticError(
                E_IO,
                "fault instruction has an invalid phase",
                {"phase": phase, "path": str(fault_file)},
            )
        when = str(instruction.get("when", "before_checkpoint"))
        aliases = {
            "after_phase": "before_checkpoint",
            "before_commit": "before_checkpoint",
            "after_commit": "after_checkpoint",
        }
        when = aliases.get(when, when)
        if when not in {"before_checkpoint", "after_checkpoint"}:
            raise DiagnosticError(
                E_IO,
                "fault instruction has an invalid checkpoint boundary",
                {"when": when, "path": str(fault_file)},
            )
        return _FaultPlan(phase=phase, when=when)

    def _validate_config_against_documents(self) -> None:
        if self.config is None:
            return
        vocabulary = self.config.vocabulary_set
        for document in self.documents:
            for term in document.terms:
                if term not in vocabulary:
                    raise DiagnosticError(
                        E_BAD_DOC,
                        "existing document uses a term absent from the new vocabulary",
                        {
                            "batch_no": document.batch_no,
                            "doc_id": document.doc_id,
                            "term": term,
                        },
                    )

    def configure(self, path: str | os.PathLike[str]) -> NormalizedConfig:
        floor = self.config.generation if self.config is not None else None
        try:
            loaded = ConfigLoader.load(path, generation_floor=floor)
        except DiagnosticError as exc:
            self._journal_failure("configure", exc)
            raise
        if self.config is not None and loaded.config.generation < self.config.generation:
            error = DiagnosticError(
                E_CONFIG_ROLLBACK,
                "configuration generation would roll back",
                {
                    "actual": loaded.config.generation,
                    "current": self.config.generation,
                },
            )
            self._journal_failure("configure", error)
            raise error
        self.config = loaded.config
        self._validate_config_against_documents()
        self._journal_success("configure", {"config_hash": self.config.config_hash})
        return self.config

    def _journal_success(
        self,
        operation: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        try:
            self.code_journal.record(
                "OK",
                "operation completed",
                operation=operation,
                details=dict(details or {}),
            )
        except DiagnosticError:
            pass

    def _journal_failure(self, operation: str, error: DiagnosticError) -> None:
        try:
            self.code_journal.record(
                error.code,
                error.message,
                operation=operation,
                details=error.details,
            )
        except DiagnosticError:
            pass

    def _analyze_new_inputs(
        self,
        batch_paths: list[str | os.PathLike[str]],
        work: dict[str, Any],
        guard: ResourceGuard,
    ) -> list[int]:
        if self.config is None:
            raise DiagnosticError(
                E_VERSION_UNSUPPORTED,
                "a configuration is required before document analysis",
            )
        completed_analyze = self.completed["analyze"]
        last_batch_no = (
            max((document.batch_no for document in self.documents), default=None)
        )
        result = analyze_jsonl_files(
            batch_paths,
            self.config,
            existing_doc_ids=[document.doc_id for document in self.documents],
            skip_batches=completed_analyze,
            last_batch_no=last_batch_no,
            resource_guard=guard,
        )
        work["observed_batches"] = list(result.observed_batches)
        work["analyzed_documents"] = result.analyzed_count
        work["skipped_analyzed_batches"] = sorted(
            set(result.observed_batches) & completed_analyze
        )
        if result.resource_actions:
            self.resource_actions.extend(result.resource_actions)
        if result.documents:
            self.documents = sort_documents([*self.documents, *result.documents])
            self.batch_info = self._build_batch_info()
            completed_analyze.update(result.observed_batches)
            completed_map = self._completed_map()
            completed_map["analyze"] = sorted(completed_analyze)
            self._commit_phase(
                "analyze",
                result.observed_batches,
                completed_override=completed_map,
            )
        all_observed = set(result.observed_batches)
        known_batches = {document.batch_no for document in self.documents}
        missing = all_observed - known_batches
        if missing:
            raise DiagnosticError(
                E_SNAPSHOT_CORRUPT,
                "observed batches are absent from analyzed state",
                {"missing_batches": sorted(missing)},
            )
        return result.observed_batches

    def _build_ledger_documents(self) -> tuple[list[dict[str, Any]], dict[str, str]]:
        entries: list[dict[str, Any]] = []
        digests: dict[str, str] = {}
        groups: dict[int, list[AnalyzedDoc]] = {}
        for document in self.documents:
            groups.setdefault(document.batch_no, []).append(document)
        for batch_no in sorted(groups):
            docs = sort_documents(groups[batch_no])
            digest = sha256_json([doc.canonical_summary() for doc in docs])
            entries.append(
                {
                    "batch_no": batch_no,
                    "doc_ids": [doc.doc_id for doc in docs],
                    "document_count": len(docs),
                    "document_digest": digest,
                }
            )
            digests[str(batch_no)] = digest
        return entries, digests

    def _self_check_internal(self, *, require_ledger: bool) -> dict[str, Any]:
        if self.config is None:
            raise DiagnosticError(
                E_VERSION_UNSUPPORTED,
                "configuration is missing during consistency check",
            )
        self._validate_config_against_documents()
        postings_report: dict[str, Any] = {
            "documents": len(self.documents),
            "terms": len(self.postings),
            "total_postings": sum(len(values) for values in self.postings.values()),
        }
        if self.documents or self.postings:
            postings_report = validate_postings_against_documents(
                self.postings,
                self.documents,
                expected_statistics=self.posting_statistics,
            )
        if self.cache is None:
            if self.postings:
                raise DiagnosticError(
                    E_SNAPSHOT_CORRUPT,
                    "postings exist without a compression cache",
                )
        else:
            params = CompressionParams.from_config(self.config)
            if self.cache.params != params:
                raise DiagnosticError(
                    E_SNAPSHOT_CORRUPT,
                    "compression cache parameters differ from configuration",
                    {
                        "cache": self.cache.params.to_dict(),
                        "config": params.to_dict(),
                    },
                )
            if self.cache.config_generation != self.config.generation:
                raise DiagnosticError(
                    E_SNAPSHOT_CORRUPT,
                    "compression cache generation differs from configuration",
                )
            if self.cache.config_version != self.config.schema_version:
                raise DiagnosticError(
                    E_SNAPSHOT_CORRUPT,
                    "compression cache schema version differs from configuration",
                )
            if set(self.cache.entries) != set(self.postings):
                raise DiagnosticError(
                    E_SNAPSHOT_CORRUPT,
                    "compression cache term set differs from postings",
                    {
                        "cache_only": sorted(
                            set(self.cache.entries) - set(self.postings)
                        ),
                        "postings_only": sorted(
                            set(self.postings) - set(self.cache.entries)
                        ),
                    },
                )
            for term in sorted(self.postings):
                entry = self.cache.entries[term]
                if entry.posting_hash != term_postings_hash(self.postings[term]):
                    raise DiagnosticError(
                        E_SNAPSHOT_CORRUPT,
                        "cache posting hash differs from live postings",
                        {"term": term},
                    )
                expected_sequence = postings_to_sequence(self.postings[term])
                if self.cache.decode_term(term) != expected_sequence:
                    raise DiagnosticError(
                        E_SNAPSHOT_CORRUPT,
                        "decoded cache sequence differs from postings",
                        {"term": term},
                    )
        expected_batches = sorted({document.batch_no for document in self.documents})
        batch_info_batches = sorted(item["batch_no"] for item in self.batch_info)
        if expected_batches != batch_info_batches:
            raise DiagnosticError(
                E_SNAPSHOT_CORRUPT,
                "batch metadata differs from analyzed documents",
                {
                    "documents": expected_batches,
                    "metadata": batch_info_batches,
                },
            )
        if self.batch_info != self._build_batch_info():
            raise DiagnosticError(
                E_SNAPSHOT_CORRUPT,
                "batch metadata content differs from analyzed documents",
            )
        if require_ledger and self.pending_replacement is not None:
            raise DiagnosticError(
                E_SNAPSHOT_CORRUPT,
                "replacement transaction is still pending",
                {"batch_no": self.pending_replacement.get("batch_no")},
            )
        ledger_report: dict[str, Any] | None = None
        if require_ledger:
            for phase in PHASES:
                missing = set(expected_batches) - self.completed[phase]
                if missing:
                    raise DiagnosticError(
                        E_SNAPSHOT_CORRUPT,
                        "checkpoint phase has not completed all batches",
                        {
                            "phase": phase,
                            "missing_batches": sorted(missing),
                        },
                    )
            if self.ledger_config_hash != self.config.config_hash:
                raise DiagnosticError(
                    E_SNAPSHOT_CORRUPT,
                    "ledger configuration marker is stale",
                )
            if self.cache is not None and (
                self.compression_config_hash != self.config.config_hash
            ):
                raise DiagnosticError(
                    E_SNAPSHOT_CORRUPT,
                    "compression cache belongs to a different configuration",
                )
            ledger = self.ledger_manager.load()
            if ledger is None:
                raise DiagnosticError(
                    E_SNAPSHOT_CORRUPT,
                    "ledger bundle is missing",
                )
            ledger_doc = ledger["ledger"]
            config_snapshot = ledger["config_snapshot"]
            stats = ledger_doc.get("statistics", {})
            if stats.get("config_hash") != self.config.config_hash:
                raise DiagnosticError(
                    E_SNAPSHOT_CORRUPT,
                    "ledger config hash differs from active configuration",
                )
            if stats.get("postings_digest") != postings_digest(self.postings):
                raise DiagnosticError(
                    E_SNAPSHOT_CORRUPT,
                    "ledger postings digest differs from live postings",
                )
            if self.cache is not None and stats.get(
                "compression_cache_checksum"
            ) != self.cache.to_dict()["checksum"]:
                raise DiagnosticError(
                    E_SNAPSHOT_CORRUPT,
                    "ledger compression checksum differs from live cache",
                )
            config_body = config_snapshot.get("config", {})
            if config_body.get("config_hash") not in (None, self.config.config_hash):
                raise DiagnosticError(
                    E_SNAPSHOT_CORRUPT,
                    "config snapshot hash differs from active configuration",
                )
            if int(config_snapshot.get("generation", -1)) != self.config.generation:
                raise DiagnosticError(
                    E_SNAPSHOT_CORRUPT,
                    "config snapshot generation differs from active configuration",
                )
            entries, digests = self._build_ledger_documents()
            if ledger_doc.get("entries") != entries:
                raise DiagnosticError(
                    E_SNAPSHOT_CORRUPT,
                    "ledger entries differ from analyzed batch metadata",
                )
            if ledger_doc.get("batch_digests") != digests:
                raise DiagnosticError(
                    E_SNAPSHOT_CORRUPT,
                    "ledger batch digests differ from analyzed documents",
                )
            ledger_report = {
                "bundle_version": ledger["bundle_version"],
                "config_snapshot_checksum": config_snapshot["checksum"],
                "ledger_checksum": ledger_doc["checksum"],
            }
        return {
            "batch_count": len(expected_batches),
            "config_hash": self.config.config_hash,
            "ledger": ledger_report,
            "postings": postings_report,
            "status": "ok",
        }

    def _run_remaining_phases(
        self,
        work: dict[str, Any],
        fault_plan: _FaultPlan | None,
    ) -> None:
        self._fault_plan = fault_plan
        guard = ResourceGuard(self.config.resource_limits)  # type: ignore[union-attr]

        completed = self.completed
        missing_postings = sorted(completed["analyze"] - completed["postings"])
        if missing_postings:
            pending_batch = (
                self.pending_replacement["batch_no"]
                if self.pending_replacement is not None
                else None
            )
            normal_missing = [
                batch for batch in missing_postings if batch != pending_batch
            ]
            if normal_missing:
                missing_docs = [
                    document
                    for document in self.documents
                    if document.batch_no in set(normal_missing)
                ]
                metrics = append_documents(self.postings, missing_docs, guard=guard)
                work["postings_added"] = work.get("postings_added", 0) + metrics[
                    "postings_added"
                ]
            if pending_batch is not None and pending_batch in missing_postings:
                metrics = rebuild_terms(
                    self.postings,
                    self.documents,
                    self.pending_replacement["affected_terms"],
                    guard=guard,
                )
                work["postings_rebuilt"] = work.get("postings_rebuilt", 0) + metrics[
                    "postings_rebuilt"
                ]
            self.posting_statistics = compute_term_statistics(self.postings)
            guard.check_postings_size(estimate_postings_bytes(self.postings))
            self.resource_actions.extend(guard.actions)
            guard.actions.clear()
            completed["postings"].update(missing_postings)
            work["posting_comparisons"] = guard.comparisons
            self._commit_phase(
                "postings",
                sorted(completed["analyze"]),
                completed_override=completed,
            )
            completed = self.completed

        targets = sorted(completed["postings"] - completed["compress"])
        force_compression = (
            self.compression_config_hash != self.config.config_hash  # type: ignore[union-attr]
            or (self.cache is None and bool(self.completed["postings"]))
        )
        if targets or force_compression:
            if force_compression:
                affected_terms = None
                reason = "build" if self.cache is None else "config_change"
            elif self.pending_replacement is not None and (
                self.pending_replacement["batch_no"] in targets
            ):
                affected_terms = set(self.pending_replacement["affected_terms"])
                reason = "batch_replacement"
            else:
                target_set = set(targets)
                affected_terms = {
                    term
                    for document in self.documents
                    if document.batch_no in target_set
                    for term, _ in document.term_frequencies
                }
                reason = "incremental_batch"
            cache, invalidation = refresh_compression_cache(
                self.postings,
                self.config,  # type: ignore[arg-type]
                previous=self.cache,
                affected_terms=affected_terms,
                reason=reason,
                report_extra={
                    "config_generation": self.config.generation,  # type: ignore[union-attr]
                    "config_hash": self.config.config_hash,  # type: ignore[union-attr]
                    "target_batches": targets,
                },
            )
            self.cache = cache
            self.compression_config_hash = self.config.config_hash  # type: ignore[union-attr]
            self.last_invalidation = invalidation
            work["compression_blocks_recomputed"] = invalidation[
                "blocks_recomputed"
            ]
            work["compression_blocks_reused"] = invalidation["blocks_reused"]
            completed["compress"].update(targets)
            self._commit_phase(
                "compress",
                sorted(completed["postings"]),
                completed_override=completed,
            )
            completed = self.completed

        missing_ledger = sorted(completed["compress"] - completed["ledger"])
        force_ledger = (
            self.ledger_config_hash != self.config.config_hash  # type: ignore[union-attr]
            or bool(missing_ledger)
        )
        if force_ledger:
            self._self_check_internal(require_ledger=False)
            entries, batch_digests = self._build_ledger_documents()
            if self.cache is None:
                empty_cache, _ = refresh_compression_cache(
                    self.postings,
                    self.config,  # type: ignore[arg-type]
                    previous=None,
                    affected_terms=[],
                    reason="empty_ledger",
                )
                self.cache = empty_cache
                self.compression_config_hash = self.config.config_hash  # type: ignore[union-attr]
            cache_document = self.cache.to_dict()
            statistics = {
                "batch_count": len(entries),
                "compression_cache_checksum": cache_document["checksum"],
                "config_generation": self.config.generation,  # type: ignore[union-attr]
                "config_hash": self.config.config_hash,
                "document_count": len(self.documents),
                "postings_digest": postings_digest(self.postings),
                "term_statistics": self.posting_statistics,
                "total_postings": sum(
                    len(sequence) for sequence in self.postings.values()
                ),
            }
            config_snapshot = {
                "config": self.config.to_dict(),  # type: ignore[union-attr]
                "config_hash": self.config.config_hash,  # type: ignore[union-attr]
                "generation": self.config.generation,  # type: ignore[union-attr]
                "logical_clock": self.config.logical_clock,  # type: ignore[union-attr]
                "schema_version": self.config.schema_version,  # type: ignore[union-attr]
                "version": max(1, self.config.generation),  # type: ignore[union-attr]
            }
            existing = self.ledger_manager.load()
            same = False
            if existing is not None:
                existing_ledger = existing["ledger"]
                existing_config = existing["config_snapshot"]
                same = (
                    existing_ledger.get("entries") == entries
                    and existing_ledger.get("statistics") == statistics
                    and existing_ledger.get("batch_digests") == batch_digests
                    and existing_config.get("config", {}).get("config_hash")
                    == self.config.config_hash  # type: ignore[union-attr]
                )
            if same:
                self.ledger_version = int(existing["bundle_version"])
                work["ledger_reused"] = True
            else:
                ledger_version = (
                    int(existing["bundle_version"]) + 1
                    if existing is not None
                    else max(1, self.ledger_version + 1)
                )
                committed = self.ledger_manager.commit(
                    ledger_version,
                    entries=entries,
                    statistics=statistics,
                    config_snapshot=config_snapshot,
                    batch_digests=batch_digests,
                )
                self.ledger_version = ledger_version
                self.metrics["ledger_commits"] = (
                    int(self.metrics.get("ledger_commits", 0)) + 1
                )
                work["ledger_version"] = committed["bundle_version"]
                work["ledger_reused"] = False
            self.ledger_config_hash = self.config.config_hash
            self.metrics["ledger_commits"] = int(
                self.metrics.get("ledger_commits", 0)
            )
            self.pending_replacement = None
            completed["ledger"].update(completed["compress"])
            self._commit_phase(
                "ledger",
                sorted(completed["compress"]),
                completed_override=completed,
            )
            completed = self.completed

        self._fault_plan = None
        self.resource_actions.extend(guard.actions)
        work["resource"] = guard.report()

    def run(
        self,
        batch_paths: Iterable[str | os.PathLike[str]] | str | os.PathLike[str] | None = None,
        *,
        config_path: str | os.PathLike[str] | None = None,
        fault_path: str | os.PathLike[str] | None = None,
        recovery_path: str | os.PathLike[str] | None = None,
    ) -> dict[str, Any]:
        operation = "run"
        work: dict[str, Any] = {
            "analyzed_documents": 0,
            "compression_blocks_recomputed": 0,
            "compression_blocks_reused": 0,
            "postings_added": 0,
            "postings_rebuilt": 0,
        }
        try:
            if config_path is not None:
                self.configure(config_path)
            if self.config is None:
                raise DiagnosticError(
                    E_VERSION_UNSUPPORTED,
                    "a v1 or v2 configuration is required",
                )
            self._validate_config_against_documents()
            if self.checkpoint is not None and self.completed["ledger"]:
                if self.ledger_manager.load() is None:
                    raise DiagnosticError(
                        E_SNAPSHOT_CORRUPT,
                        "checkpoint claims a completed ledger but the ledger is missing",
                    )
            if isinstance(batch_paths, (str, os.PathLike)):
                path_list = [batch_paths]
            elif batch_paths is None:
                path_list = []
            else:
                path_list = list(batch_paths)
            fault_plan = self._load_fault_plan(fault_path, recovery_path)
            if recovery_path is not None:
                self.metrics["recoveries"] = int(
                    self.metrics.get("recoveries", 0)
                ) + 1
                work["recovery_instruction"] = str(recovery_path)
                work["checkpoint_version_at_recovery"] = (
                    self.checkpoint.version if self.checkpoint else 0
                )
            self._fault_plan = fault_plan
            guard = ResourceGuard(self.config.resource_limits)
            if path_list:
                self._analyze_new_inputs(path_list, work, guard)
            elif self.checkpoint is None:
                raise DiagnosticError(
                    E_BAD_DOC,
                    "no batch input or checkpoint is available",
                    {"doc_id": None},
                )
            self._run_remaining_phases(work, fault_plan)
            if self.cache is not None:
                self.cache.save(self.cache_path)
            atomic_write_json(
                self.invalidation_path,
                {
                    "checkpoint_version": self.checkpoint.version
                    if self.checkpoint
                    else 0,
                    "report": self.last_invalidation,
                },
            )
            self._journal_success(
                operation,
                {
                    "checkpoint_version": self.checkpoint.version
                    if self.checkpoint
                    else 0,
                    "completed_batches": sorted(
                        self.completed["ledger"] if self.checkpoint else []
                    ),
                },
            )
            result = self.status()
            result["ok"] = True
            result["work"] = work
            return result
        except DiagnosticError as exc:
            self._safe_reload_checkpoint_state()
            self._journal_failure(operation, exc)
            raise
        except InjectedFault as exc:
            try:
                self.code_journal.record(
                    exc.code,
                    str(exc),
                    operation=operation,
                    details={"phase": exc.phase, "when": exc.when},
                )
            except DiagnosticError:
                pass
            raise

    def replace_batch(
        self,
        batch_no: int,
        batch_path: str | os.PathLike[str],
        *,
        fault_path: str | os.PathLike[str] | None = None,
        recovery_path: str | os.PathLike[str] | None = None,
    ) -> dict[str, Any]:
        operation = "replace_batch"
        work: dict[str, Any] = {
            "analyzed_documents": 0,
            "compression_blocks_recomputed": 0,
            "compression_blocks_reused": 0,
            "postings_added": 0,
            "postings_rebuilt": 0,
        }
        try:
            if self.config is None or self.checkpoint is None:
                raise DiagnosticError(
                    E_BAD_DOC,
                    "batch replacement requires an existing checkpoint",
                    {"batch_no": batch_no, "doc_id": None},
                )
            if self.completed["ledger"] and self.ledger_manager.load() is None:
                raise DiagnosticError(
                    E_SNAPSHOT_CORRUPT,
                    "ledger is missing before batch replacement",
                )
            if isinstance(batch_no, bool) or not isinstance(batch_no, int) or batch_no < 0:
                raise DiagnosticError(
                    E_BAD_DOC,
                    "batch_no must be a non-negative integer",
                    {"batch_no": batch_no, "doc_id": None},
                )
            old_documents = [
                document
                for document in self.documents
                if document.batch_no == batch_no
            ]
            if not old_documents:
                raise DiagnosticError(
                    E_BAD_DOC,
                    "batch to replace does not exist",
                    {"batch_no": batch_no, "doc_id": None},
                )
            other_documents = [
                document
                for document in self.documents
                if document.batch_no != batch_no
            ]
            guard = ResourceGuard(self.config.resource_limits)
            analyzed = analyze_jsonl_files(
                [batch_path],
                self.config,
                existing_doc_ids=[document.doc_id for document in other_documents],
                last_batch_no=None,
                resource_guard=guard,
            )
            if analyzed.observed_batches != [batch_no]:
                raise DiagnosticError(
                    E_BAD_DOC,
                    "replacement file must contain exactly the target batch",
                    {
                        "batch_no": batch_no,
                        "observed_batches": analyzed.observed_batches,
                        "doc_id": None,
                    },
                )
            if not analyzed.documents:
                raise DiagnosticError(
                    E_BAD_DOC,
                    "replacement batch has no documents",
                    {"batch_no": batch_no, "doc_id": None},
                )
            old_terms = {
                term for document in old_documents for term, _ in document.term_frequencies
            }
            new_terms = {
                term
                for document in analyzed.documents
                for term, _ in document.term_frequencies
            }
            affected_terms = sorted(old_terms | new_terms)
            self.documents = sort_documents(
                [*other_documents, *analyzed.documents]
            )
            self.batch_info = self._build_batch_info()
            old_doc_ids = [document.doc_id for document in old_documents]
            new_doc_ids = [document.doc_id for document in analyzed.documents]
            self.pending_replacement = {
                "affected_terms": affected_terms,
                "batch_no": batch_no,
                "new_doc_ids": new_doc_ids,
                "old_doc_ids": old_doc_ids,
                "reason": "batch_replacement",
            }
            replacement_completed = self._completed_map()
            for phase in PHASES:
                replacement_completed[phase] = [
                    value
                    for value in replacement_completed[phase]
                    if value != batch_no
                ]
            replacement_completed["analyze"] = sorted(
                {*replacement_completed["analyze"], batch_no}
            )
            if analyzed.resource_actions:
                self.resource_actions.extend(analyzed.resource_actions)
            work["analyzed_documents"] = len(analyzed.documents)
            work["replaced_batch"] = batch_no
            work["affected_terms"] = affected_terms
            fault_plan = self._load_fault_plan(fault_path, recovery_path)
            self._fault_plan = fault_plan
            self._commit_phase(
                "analyze",
                [batch_no],
                completed_override=replacement_completed,
            )
            self._run_remaining_phases(work, fault_plan)
            if self.cache is not None:
                self.cache.save(self.cache_path)
            atomic_write_json(
                self.invalidation_path,
                {
                    "checkpoint_version": self.checkpoint.version
                    if self.checkpoint
                    else 0,
                    "report": self.last_invalidation,
                },
            )
            self._journal_success(operation, {"batch_no": batch_no})
            result = self.status()
            result["ok"] = True
            result["work"] = work
            result["invalidation"] = self.last_invalidation
            return result
        except DiagnosticError as exc:
            self._safe_reload_checkpoint_state()
            self._journal_failure(operation, exc)
            raise
        except InjectedFault:
            raise

    def self_check(self) -> dict[str, Any]:
        try:
            result = self._self_check_internal(require_ledger=True)
            self._journal_success("self_check", {"config_hash": result["config_hash"]})
            return result
        except DiagnosticError as exc:
            self._journal_failure("self_check", exc)
            raise

    def query_term(self, term: str) -> dict[str, Any]:
        operation = "query_term"
        try:
            if self.config is None:
                raise DiagnosticError(
                    E_TERM_NOT_FOUND,
                    "term is unavailable before configuration is loaded",
                    {"term": term},
                )
            try:
                normalized = normalize_term(term)
            except (TypeError, ValueError) as exc:
                raise DiagnosticError(
                    E_TERM_NOT_FOUND,
                    "term is invalid",
                    {"term": term},
                ) from exc
            if normalized not in self.config.vocabulary_set:
                raise DiagnosticError(
                    E_TERM_NOT_FOUND,
                    "term is outside the controlled vocabulary",
                    {"term": normalized},
                )
            sequence = self.postings.get(normalized, [])
            blocks = self.cache.block_status(normalized) if self.cache else []
            result = {
                "blocks": blocks,
                "doc_frequency": len(sequence),
                "postings": [posting.to_dict() for posting in sequence],
                "term": normalized,
                "term_frequency": sum(len(posting.positions) for posting in sequence),
            }
            self._journal_success(operation, {"term": normalized})
            return result
        except DiagnosticError as exc:
            self._journal_failure(operation, exc)
            raise

    def query_block(self, term: str, block_index: int) -> dict[str, Any]:
        operation = "query_block"
        try:
            if self.cache is None:
                raise DiagnosticError(
                    E_TERM_NOT_FOUND,
                    "compression cache is not available",
                    {"term": term, "block": block_index},
                )
            try:
                normalized = normalize_term(term)
            except (TypeError, ValueError) as exc:
                raise DiagnosticError(
                    E_TERM_NOT_FOUND,
                    "term is invalid",
                    {"term": term},
                ) from exc
            values = self.cache.decode_block(normalized, block_index)
            status = self.cache.block_status(normalized)[block_index]
            result = {
                "block": status,
                "term": normalized,
                "values": values,
            }
            self._journal_success(
                operation, {"term": normalized, "block": block_index}
            )
            return result
        except DiagnosticError as exc:
            self._journal_failure(operation, exc)
            raise

    def last_business_code(self) -> dict[str, Any] | None:
        return self.code_journal.read()

    def status(self) -> dict[str, Any]:
        cache_checksum = None
        if self.cache is not None:
            cache_checksum = self.cache.to_dict()["checksum"]
        ledger = None
        try:
            ledger_value = self.ledger_manager.load()
            if ledger_value is not None:
                ledger = {
                    "bundle_version": ledger_value["bundle_version"],
                    "ledger_checksum": ledger_value["ledger"]["checksum"],
                    "config_snapshot_checksum": ledger_value["config_snapshot"][
                        "checksum"
                    ],
                }
        except DiagnosticError:
            ledger = {"corrupt": True}
        return {
            "batch_info": self.batch_info,
            "cache": {
                "checksum": cache_checksum,
                "config_generation": self.cache.config_generation
                if self.cache
                else None,
                "params": self.cache.params.to_dict() if self.cache else None,
                "terms": len(self.cache.entries) if self.cache else 0,
            },
            "checkpoint": {
                "completed_by_phase": {
                    phase: sorted(self.completed[phase]) for phase in PHASES
                },
                "phase": self.checkpoint.phase if self.checkpoint else None,
                "version": self.checkpoint.version if self.checkpoint else 0,
            },
            "config": self.config.snapshot_payload() if self.config else None,
            "invalidation": self.last_invalidation,
            "ledger": ledger,
            "metrics": self.metrics,
            "pending_replacement": self.pending_replacement,
            "postings": {
                "documents": len(self.documents),
                "terms": len(self.postings),
                "total_postings": sum(
                    len(sequence) for sequence in self.postings.values()
                ),
            },
            "resource_actions": self.resource_actions,
        }
