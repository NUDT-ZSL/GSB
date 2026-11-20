"""End-to-end build, recovery, query, and diagnostic orchestration."""

from __future__ import annotations

import os
import signal
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .bridge import affected_partitions
from .data import Query, read_documents
from .errors import format_corrupt, resource_limit
from .expand import ExpansionConfig, expand_query
from .indexer import (
    build_partition_worker,
    document_set_checksum,
    partition_languages,
    partition_payload,
    retag_partition,
)
from .models import BridgeResource, RuleSet
from .scoring import RetrievalConfig, search
from .snapshots import SnapshotStore, SnapshotView, checksum_from_bytes
from .util import checksum_object, stable_json_bytes


@dataclass(frozen=True)
class BuildConfig:
    workers: int = 4
    max_postings: int = 1_000_000
    max_parallel_partitions: int = 8
    parallel_policy: str = "degrade"
    crash_after_partitions: int | None = None

    def validate(self) -> None:
        if self.workers < 1:
            raise format_corrupt("workers must be positive", workers=self.workers)
        if self.max_postings < 1:
            raise format_corrupt("max_postings must be positive")
        if self.max_parallel_partitions < 1:
            raise format_corrupt("max_parallel_partitions must be positive")
        if self.parallel_policy not in {"degrade", "error"}:
            raise format_corrupt(
                "parallel_policy must be degrade or error",
                parallel_policy=self.parallel_policy,
            )
        if (
            self.crash_after_partitions is not None
            and self.crash_after_partitions < 1
        ):
            raise format_corrupt("crash_after_partitions must be positive")


@dataclass(frozen=True)
class BuildResult:
    commit: Mapping[str, Any]
    index_manifest: Mapping[str, Any]
    build_id: str
    resumed: bool


def build_and_commit(
    store: SnapshotStore,
    *,
    documents_path: str | Path,
    ruleset: RuleSet,
    rule_version: int,
    bridge: BridgeResource,
    index_version: int,
    config: BuildConfig | None = None,
) -> BuildResult:
    cfg = config or BuildConfig()
    cfg.validate()
    if rule_version < 1 or index_version < 1:
        raise format_corrupt("snapshot versions must be positive")

    located_documents = read_documents(documents_path)
    docs_checksum = document_set_checksum(located_documents)
    languages = partition_languages(located_documents)
    documents_by_lang: dict[str, list[dict[str, Any]]] = {lang: [] for lang in languages}
    for located in located_documents:
        documents_by_lang[located.document.doc_lang].append(partition_payload(located))
    for payloads in documents_by_lang.values():
        payloads.sort(key=lambda item: item["doc_id"])

    rule_manifest = store.save_rules_snapshot(ruleset, rule_version)
    bridge_manifest = store.save_bridge_snapshot(bridge)
    previous = store.load_current_optional()

    if previous is not None:
        previous_index_docs = previous.index_manifest.get("docs_checksum")
        if previous_index_docs == docs_checksum:
            candidate_affected = set(affected_partitions(previous.bridge, bridge))
        else:
            candidate_affected = set(languages)
    else:
        candidate_affected = set(languages)
    target_affected = sorted(candidate_affected & set(languages))

    base_id = previous.commit_id if previous is not None else "initial"
    build_id = checksum_object(
        {
            "base_commit": base_id,
            "docs_checksum": docs_checksum,
        }
    )[:20]
    state = store.load_build_state(build_id)
    resumed = state is not None and state.get("status") != "committed"

    if state is None or state.get("status") == "committed":
        state = {
            "kind": "build_state",
            "build_id": build_id,
            "base_commit": base_id,
            "docs_checksum": docs_checksum,
            "bridge_version": bridge.version,
            "bridge_checksum": bridge.checksum,
            "index_version": index_version,
            "completed": {},
            "recomputed_this_run": [],
            "status": "building",
        }
        if previous is not None and previous.index_manifest["docs_checksum"] == docs_checksum:
            reusable_payload: dict[str, Mapping[str, Any]] = {}
            old_index_manifest, old_partitions = store.load_index_snapshot(
                previous.index_manifest["version"]
            )
            del old_index_manifest
            for lang in languages:
                if lang in target_affected:
                    continue
                if lang not in old_partitions:
                    continue
                payload = old_partitions[lang]
                data = stable_json_bytes(payload)
                built_checksum = store.write_build_partition(build_id, lang, data)
                state["completed"][lang] = {
                    "checksum": built_checksum,
                    "posting_count": len(payload.get("postings", [])),
                    "reused_from_index_version": previous.index_manifest["version"],
                }
                reusable_payload[lang] = payload
            del reusable_payload
        store.save_build_state(build_id, state)
    else:
        if state.get("docs_checksum") != docs_checksum:
            state = {
                "kind": "build_state",
                "build_id": build_id,
                "base_commit": base_id,
                "docs_checksum": docs_checksum,
                "bridge_version": bridge.version,
                "bridge_checksum": bridge.checksum,
                "index_version": index_version,
                "completed": {},
                "recomputed_this_run": [],
                "status": "building",
            }
        elif state.get("bridge_checksum") != bridge.checksum:
            old_bridge_manifest, old_bridge = store.load_bridge_snapshot(
                int(state["bridge_version"])
            )
            del old_bridge_manifest
            recovery_affected = set(affected_partitions(old_bridge, bridge))
            for lang in sorted(recovery_affected):
                state["completed"].pop(lang, None)
            state["bridge_version"] = bridge.version
            state["bridge_checksum"] = bridge.checksum
        state["index_version"] = index_version
        state["status"] = "building"
        store.save_build_state(build_id, state)

    completed: dict[str, dict[str, Any]] = dict(state.get("completed", {}))
    for lang in list(completed):
        record = completed[lang]
        path = store.build_partition_path(build_id, lang)
        if not path.exists():
            completed.pop(lang)
            continue
        if checksum_from_bytes(path.read_bytes()) != record.get("checksum"):
            completed.pop(lang)
    state["completed"] = completed
    pending = [lang for lang in languages if lang not in completed]

    degraded: list[str] = []
    parallel_scope_count = len(target_affected)
    if parallel_scope_count > cfg.max_parallel_partitions:
        if cfg.parallel_policy == "error":
            raise resource_limit(
                "parallel partition recompute budget exceeded",
                affected_partitions=parallel_scope_count,
                max_parallel_partitions=cfg.max_parallel_partitions,
            )
        degraded.append("DEGRADED_LIMITED_PARALLELISM")

    reused_posting_count = sum(
        int(record.get("posting_count", 0))
        for lang, record in completed.items()
        if lang not in pending
    )
    if reused_posting_count > cfg.max_postings:
        raise resource_limit(
            "reused posting budget exceeded",
            max_postings=cfg.max_postings,
            actual_postings=reused_posting_count,
        )

    recomputed_this_run: list[str] = []
    if pending:
        recomputed_this_run = _build_pending(
            store,
            build_id=build_id,
            pending=pending,
            documents_by_lang=documents_by_lang,
            index_version=index_version,
            bridge=bridge,
            rule_manifest=rule_manifest,
            state=state,
            cfg=cfg,
            already_postings=reused_posting_count,
        )
        state = store.load_build_state(build_id) or state
        completed = dict(state["completed"])

    retagged: dict[str, Mapping[str, Any]] = {}
    total_postings = 0
    for lang in languages:
        payload = store.read_build_partition(build_id, lang)
        normalized = retag_partition(
            payload,
            index_version=index_version,
            bridge_version=bridge.version,
            bridge_checksum=bridge.checksum,
            rule_version=rule_version,
            rule_checksum=rule_manifest["checksum"],
        )
        retagged[lang] = normalized
        total_postings += len(normalized["postings"])
    if total_postings > cfg.max_postings:
        raise resource_limit(
            "posting budget exceeded",
            max_postings=cfg.max_postings,
            actual_postings=total_postings,
        )

    index_manifest = store.save_index_snapshot(
        index_version=index_version,
        partitions=retagged,
        rule_manifest=rule_manifest,
        bridge_manifest=bridge_manifest,
        docs_checksum=docs_checksum,
        degraded=degraded,
    )
    recomputed = sorted(set(recomputed_this_run) & set(languages))
    reused = sorted(set(languages) - set(recomputed))
    commit = store.commit(
        rule_manifest=rule_manifest,
        bridge_manifest=bridge_manifest,
        index_manifest=index_manifest,
        affected_partitions=target_affected,
        reused_partitions=reused,
        recomputed_partitions=recomputed,
        degraded=degraded,
    )
    state["status"] = "committed"
    state["commit_id"] = commit["commit_id"]
    store.save_build_state(build_id, state)
    return BuildResult(
        commit=commit,
        index_manifest=index_manifest,
        build_id=build_id,
        resumed=resumed,
    )


def _build_pending(
    store: SnapshotStore,
    *,
    build_id: str,
    pending: list[str],
    documents_by_lang: Mapping[str, list[dict[str, Any]]],
    index_version: int,
    bridge: BridgeResource,
    rule_manifest: Mapping[str, Any],
    state: dict[str, Any],
    cfg: BuildConfig,
    already_postings: int,
) -> list[str]:
    payloads = [
        {
            "partition_lang": lang,
            "documents": documents_by_lang[lang],
            "index_version": index_version,
            "bridge_version": bridge.version,
            "bridge_checksum": bridge.checksum,
            "rule_version": rule_manifest["version"],
            "rule_checksum": rule_manifest["checksum"],
        }
        for lang in pending
    ]
    effective_workers = min(
        cfg.workers,
        cfg.max_parallel_partitions,
        max(1, len(pending)),
    )
    total_postings = already_postings
    completed_now: list[str] = []

    def consume(
        results: Any,
        executor: ProcessPoolExecutor | None = None,
    ) -> None:
        nonlocal total_postings
        for lang, data, posting_count in results:
            total_postings += posting_count
            if total_postings > cfg.max_postings:
                raise resource_limit(
                    "posting budget exceeded during partition build",
                    max_postings=cfg.max_postings,
                    actual_postings=total_postings,
                    partition_lang=lang,
                )
            built_checksum = store.write_build_partition(build_id, lang, data)
            state["completed"][lang] = {
                "checksum": built_checksum,
                "posting_count": posting_count,
                "reused_from_index_version": None,
            }
            if lang not in state["recomputed_this_run"]:
                state["recomputed_this_run"].append(lang)
            completed_now.append(lang)
            store.save_build_state(build_id, state)
            if (
                cfg.crash_after_partitions is not None
                and len(completed_now) >= cfg.crash_after_partitions
            ):
                # Deliberately skip cleanup and final commit to exercise recovery.
                _force_exit(executor)

    if effective_workers == 1:
        consume(build_partition_worker(payload) for payload in payloads)
    else:
        with ProcessPoolExecutor(max_workers=effective_workers) as executor:
            consume(executor.map(build_partition_worker, payloads), executor)
    return completed_now


def _force_exit(executor: ProcessPoolExecutor | None) -> None:
    """Terminate the whole local worker set, then abort without finalization.

    A plain ``os._exit`` can leave multiprocessing workers holding inherited
    stdout pipes, which defeats subprocess-based recovery tests. Killing those
    workers is still an ungraceful termination: no snapshot or state rollback is
    performed here.
    """

    if executor is not None:
        processes = getattr(executor, "_processes", {}) or {}
        for process in list(processes.values()):
            try:
                if process.pid is not None:
                    os.kill(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    os._exit(97)


def query_view(
    view: SnapshotView,
    query: Query,
    *,
    expansion_config: ExpansionConfig | None = None,
    retrieval_config: RetrievalConfig | None = None,
) -> dict[str, Any]:
    expansion = expand_query(
        query,
        view.rules,
        view.bridge,
        config=expansion_config,
    )
    candidates = expansion["candidates"]
    result = search(
        query,
        candidates,
        partition_langs=[
            entry["lang"] for entry in view.index_manifest["partitions"]
        ],
        partition_loader=view.partition_loader(),
        total_postings=view.index_manifest["posting_count"],
        total_decoded_bytes=view.index_manifest["decoded_bytes"],
        index_version=view.index_manifest["version"],
        rule_version=view.rule_manifest["version"],
        rule_checksum=view.rule_manifest["checksum"],
        bridge_version=view.bridge_manifest["version"],
        bridge_checksum=view.bridge_manifest["checksum"],
        config=retrieval_config,
    )
    result["expansion"] = expansion
    return result
