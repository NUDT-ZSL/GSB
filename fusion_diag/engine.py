from __future__ import annotations

import hashlib
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from .cache import ContentAddressedCache
from .canonical import (
    atomic_write_bytes,
    canonical_bytes,
    digest_file,
    digest_json,
)
from .config import (
    ROUTE_ORDER,
    ConfigSnapshot,
    classify_config_change,
    load_config,
)
from .errors import CandidateDrop, FormatCorrupt
from .fusion import (
    align_candidates,
    finalize_fusion,
    fusion_cache_key,
)
from .ingest import (
    PreparedCorpus,
    Query,
    load_documents,
    load_queries,
    load_vocabulary,
    prepare_corpus,
)
from .recall import (
    RECALLERS,
    route_cache_plan,
    route_result_hash,
)
from .resources import ResourceGuard
from .state import StateStore


STAGES = ("ingest", "recall", "align", "fusion", "commit")


class SimulatedCrash(RuntimeError):
    def __init__(self, stage: str) -> None:
        super().__init__(f"simulated crash after stage {stage}")
        self.stage = stage


@dataclass
class Workspace:
    corpus: PreparedCorpus
    queries: tuple[Query, ...]
    query_by_id: dict[str, Query]
    config: ConfigSnapshot
    document_file_hash: str
    query_file_hash: str
    queries_content_hash: str

    @property
    def input_hash(self) -> str:
        return digest_json(
            {
                "documents": self.corpus.content_hash,
                "queries": self.queries_content_hash,
                "glossary": self.corpus.glossary_hash,
            }
        )


@dataclass
class QueryPlan:
    query: Query
    query_content_hash: str
    route_keys: dict[str, str]
    route_dependency_hashes: dict[str, str]
    dependency_hash: str


def load_workspace(
    *,
    docs_path: str | Path,
    queries_path: str | Path,
    vocab_path: str | Path,
    config_path: str | Path,
) -> Workspace:
    vocab = load_vocabulary(vocab_path)
    documents = load_documents(docs_path, vocab)
    corpus = prepare_corpus(documents, vocab)
    queries = load_queries(queries_path, categories=corpus.categories)
    config = load_config(config_path)
    return Workspace(
        corpus=corpus,
        queries=queries,
        query_by_id={query.query_id: query for query in queries},
        config=config,
        document_file_hash=digest_file(docs_path),
        query_file_hash=digest_file(queries_path),
        queries_content_hash=digest_json(
            [query.content_dict() for query in queries]
        ),
    )


def build_query_plan(
    corpus: PreparedCorpus, query: Query, config: ConfigSnapshot
) -> QueryPlan:
    plan = route_cache_plan(corpus, query, config)
    return QueryPlan(
        query=query,
        query_content_hash=plan["query_content_hash"],
        route_keys={
            route: plan["routes"][route]["cache_key"]
            for route in ROUTE_ORDER
        },
        route_dependency_hashes={
            route: plan["routes"][route]["dependency_hash"]
            for route in ROUTE_ORDER
        },
        dependency_hash=plan["dependency_hash"],
    )


def _disabled_route_result(
    route: str, route_config: dict[str, Any]
) -> dict[str, Any]:
    return {
        "route": route,
        "enabled": False,
        "limit": int(route_config["limit"]),
        "candidates": [],
    }


def _recall_routes(
    corpus: PreparedCorpus,
    query: Query,
    config: ConfigSnapshot,
    plan: QueryPlan,
    cache: ContentAddressedCache,
    *,
    use_cache: bool,
) -> dict[str, Any]:
    routes: dict[str, Any] = {}
    for route in ROUTE_ORDER:
        key = plan.route_keys[route]
        result = cache.get_route(key) if use_cache else None
        if result is None:
            route_config = config.recalls[route]
            if route_config["enabled"]:
                result = RECALLERS[route](corpus, query, route_config)
            else:
                result = _disabled_route_result(route, route_config)
            cache.put_route(key, result)
        routes[route] = result
    return routes


def _upstream_hash(routes: dict[str, Any]) -> str:
    return digest_json(
        {
            route: route_result_hash(routes[route])
            for route in ROUTE_ORDER
        }
    )


def _core_from_fused(
    routes: dict[str, Any], fused: dict[str, Any]
) -> dict[str, Any]:
    trace = fused["trace"]
    return {
        "normalization": fused["normalization"],
        "recalls": routes,
        "alignment": {
            "candidates": fused["candidates"],
            "trace": trace,
        },
        "top_k": fused["top_k"],
        "diagnostics": {
            "normalization": fused["normalization"],
            "candidate_count": len(fused["candidates"]),
            "route_candidate_counts": trace["route_candidate_counts"],
            "normalization_calculations": trace[
                "normalization_calculations"
            ],
            "fusion_weight_count": trace["fusion_weight_count"],
        },
    }


def _compute_core(
    corpus: PreparedCorpus,
    plan: QueryPlan,
    config: ConfigSnapshot,
    cache: ContentAddressedCache,
    *,
    use_cache: bool,
) -> tuple[dict[str, Any], str, str]:
    routes = _recall_routes(
        corpus,
        plan.query,
        config,
        plan,
        cache,
        use_cache=use_cache,
    )
    upstream = _upstream_hash(routes)
    fusion_key = fusion_cache_key(
        documents_content_hash=plan.dependency_hash,
        query_content_hash=plan.query_content_hash,
        config_digest=config.digest,
        upstream_node_hash=upstream,
    )
    core = cache.get_fusion(fusion_key) if use_cache else None
    if core is None:
        alignment = align_candidates(plan.query, routes, config)
        fused = finalize_fusion(plan.query, alignment, config)
        core = _core_from_fused(routes, fused)
        cache.put_fusion(fusion_key, core)
    return core, upstream, fusion_key


def _core_from_result(result: dict[str, Any]) -> dict[str, Any]:
    try:
        return {
            "normalization": result["diagnostics"]["normalization"],
            "recalls": result["recalls"],
            "alignment": result["alignment"],
            "top_k": result["top_k"],
            "diagnostics": result["diagnostics"],
        }
    except KeyError as exc:
        raise FormatCorrupt(
            f"committed result is missing core field {exc}", field="state.result"
        ) from exc


def _record_from_core(
    plan: QueryPlan,
    config: ConfigSnapshot,
    core: dict[str, Any],
) -> dict[str, Any]:
    routes = core["recalls"]
    upstream = _upstream_hash(routes)
    fusion_key = fusion_cache_key(
        documents_content_hash=plan.dependency_hash,
        query_content_hash=plan.query_content_hash,
        config_digest=config.digest,
        upstream_node_hash=upstream,
    )
    candidates = core["alignment"]["candidates"]
    return {
        "schema_version": "fusion_diagnostic.query.v1",
        "query_id": plan.query.query_id,
        "config": config.reference(),
        "query": plan.query.to_dict(),
        "recalls": routes,
        "alignment": core["alignment"],
        "top_k": core["top_k"],
        "diagnostics": {
            **core["diagnostics"],
            "document_dependency_hash": plan.dependency_hash,
            "query_content_hash": plan.query_content_hash,
            "fused_candidate_count": len(candidates),
        },
        "cache": {
            "documents_content_hash": plan.dependency_hash,
            "query_content_hash": plan.query_content_hash,
            "config_parameter_version": config.digest,
            "route_cache_keys": plan.route_keys,
            "route_dependency_hashes": plan.route_dependency_hashes,
            "upstream_node_hash": upstream,
            "fusion_cache_key": fusion_key,
        },
    }


def _used_routes(result: dict[str, Any]) -> set[str]:
    used: set[str] = set()
    for candidate in result.get("alignment", {}).get("candidates", []):
        for source in candidate.get("sources", []):
            route = source.get("route")
            if isinstance(route, str):
                used.add(route)
    return used


def decide_reuse(
    plan: QueryPlan,
    config: ConfigSnapshot,
    committed: dict[str, Any] | None,
    *,
    force_full: bool,
) -> tuple[bool, str]:
    if force_full:
        return False, "full_recompute"
    if committed is None:
        return False, "not_committed"
    if committed["query_content_hash"] != plan.query_content_hash:
        return False, "query_content_changed"
    old_route_keys = committed["route_keys"]
    changed_routes = [
        route
        for route in ROUTE_ORDER
        if old_route_keys.get(route) != plan.route_keys[route]
    ]
    if changed_routes:
        return False, "route_dependencies_changed:" + ",".join(changed_routes)
    change = classify_config_change(
        committed["config_payload"], config.payload
    )
    if change["kind"] == "none":
        return True, "unchanged"
    if change["kind"] == "route_weight":
        used = _used_routes(committed["result"])
        if not any(route in used for route in change["routes"]):
            return True, "unused_route_weight_changed"
        return False, "used_route_weight_changed:" + ",".join(change["routes"])
    if change["kind"] == "normalization":
        return False, "normalization_changed"
    return False, "config_changed"


def _parallel_map(
    items: list[Query],
    worker: Callable[[Query], tuple[Query, Any]],
    workers: int,
) -> dict[str, Any]:
    if workers <= 1 or len(items) <= 1:
        return {query.query_id: value for query, value in map(worker, items)}
    with ThreadPoolExecutor(max_workers=min(workers, len(items))) as executor:
        results = list(executor.map(worker, items))
    return {query.query_id: value for query, value in results}


def _estimates(corpus: PreparedCorpus) -> int:
    return len(canonical_bytes(corpus.prepared_payload()))


def _checkpoint_summary(checkpoint: dict[str, Any] | None) -> dict[str, Any] | None:
    if checkpoint is None:
        return None
    return {
        "version": checkpoint["version"],
        "stage": checkpoint["stage"],
        "completed_query_count": len(checkpoint["completed_query_ids"]),
        "checksum": checkpoint["checksum"],
    }


def _write_jsonl(path: str | Path, records: Iterable[dict[str, Any]]) -> str:
    lines = [canonical_bytes(record) for record in records]
    data = b"\n".join(lines) + (b"\n" if lines else b"")
    atomic_write_bytes(path, data)
    return hashlib.sha256(data).hexdigest()


def _output_records(
    workspace: Workspace, results: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    missing = [
        query.query_id
        for query in workspace.queries
        if query.query_id not in results
    ]
    if missing:
        raise CandidateDrop(
            "batch output is missing query results",
            doc_id=None,
        )
    return [results[query.query_id] for query in workspace.queries]


def _verify_full(
    workspace: Workspace,
    workdir: Path,
    output_records: list[dict[str, Any]],
) -> dict[str, Any]:
    verify_root = workdir / "_verify_full"
    cache = ContentAddressedCache(verify_root / "cache")
    results: dict[str, dict[str, Any]] = {}
    for query in workspace.queries:
        plan = build_query_plan(workspace.corpus, query, workspace.config)
        core, _, _ = _compute_core(
            workspace.corpus,
            plan,
            workspace.config,
            cache,
            use_cache=False,
        )
        results[query.query_id] = _record_from_core(
            plan, workspace.config, core
        )
    full_records = _output_records(workspace, results)
    incremental_by_id = {record["query_id"]: canonical_bytes(record) for record in output_records}
    full_by_id = {
        record["query_id"]: canonical_bytes(record) for record in full_records
    }
    mismatches = [
        query_id
        for query_id in sorted(set(incremental_by_id) | set(full_by_id))
        if incremental_by_id.get(query_id) != full_by_id.get(query_id)
    ]
    return {
        "status": "match" if not mismatches else "mismatch",
        "compared_queries": len(full_records),
        "byte_identical_queries": len(full_records) - len(mismatches),
        "mismatch_query_ids": mismatches,
        "full_output_sha256": hashlib.sha256(
            b"\n".join(full_by_id[record["query_id"]] for record in full_records)
            + b"\n"
        ).hexdigest(),
    }


def run_batch(
    workspace: Workspace,
    *,
    workdir: str | Path,
    output_path: str | Path,
    mode: str = "auto",
    use_cache: bool = True,
    resume: bool = True,
    crash_after: str | None = None,
    verify_full: bool = False,
) -> dict[str, Any]:
    if mode not in {"auto", "full"}:
        raise ValueError("mode must be auto or full")
    started = time.perf_counter()
    root = Path(workdir)
    state = StateStore(root / "state")
    cache = ContentAddressedCache(root / "cache")
    guard = ResourceGuard(
        workspace.config.fusion["memory_limit_mb"],
        worker_count=workspace.config.fusion["worker_count"],
    )
    plans = {
        query.query_id: build_query_plan(
            workspace.corpus, query, workspace.config
        )
        for query in workspace.queries
    }
    guard.check("ingest", _estimates(workspace.corpus))
    committed = state.get_all_committed()
    affected: list[str] = []
    reused: list[str] = []
    decisions: dict[str, str] = {}
    reused_records: dict[str, dict[str, Any]] = {}
    for query in workspace.queries:
        query_id = query.query_id
        old = committed.get(query_id)
        reuse, reason = decide_reuse(
            plans[query_id],
            workspace.config,
            old,
            force_full=mode == "full",
        )
        decisions[query_id] = reason
        if not reuse:
            affected.append(query_id)
            continue
        reused.append(query_id)
        if (
            old is not None
            and old["config_digest"] == workspace.config.digest
            and old["config_payload"] == workspace.config.payload
            and old["envelope"] == workspace.config.reference()
        ):
            reused_records[query_id] = old["result"]
        elif old is not None:
            core = _core_from_result(old["result"])
            record = _record_from_core(
                plans[query_id], workspace.config, core
            )
            cache.put_fusion(record["cache"]["fusion_cache_key"], core)
            reused_records[query_id] = record
    affected = sorted(set(affected))
    reused = sorted(set(reused))
    run_key = digest_json(
        {
            "schema": "batch_run.v1",
            "input_hash": workspace.input_hash,
            "config_digest": workspace.config.digest,
            "config_reference": workspace.config.reference(),
            "mode": mode,
            "use_cache": use_cache,
        }
    )
    latest = state.latest_checkpoint(run_key) if resume else None
    resumed_from = _checkpoint_summary(latest)
    if latest is not None and latest["stage"] == "commit":
        results: dict[str, dict[str, Any]] = {}
        valid_checkpoint = True
        for query in workspace.queries:
            row = state.get_committed(query.query_id)
            plan = plans[query.query_id]
            if (
                row is None
                or row["query_content_hash"] != plan.query_content_hash
                or row["route_keys"] != plan.route_keys
                or row["config_payload"] != workspace.config.payload
                or row["config_digest"] != workspace.config.digest
                or row["envelope"] != workspace.config.reference()
            ):
                valid_checkpoint = False
                break
            results[query.query_id] = row["result"]
        if not valid_checkpoint:
            latest = None
            resumed_from = None
        else:
            affected = []
            reused = sorted(results)
            decisions = {query_id: "checkpoint_commit" for query_id in reused}
            reused_records = results
    workers = int(workspace.config.fusion["worker_count"])
    stage_payload: dict[str, Any] = {}
    start_index = 0
    if latest is not None:
        start_index = STAGES.index(latest["stage"]) + 1
        stage_payload = latest["payload"]
    routes_by_id: dict[str, dict[str, Any]] = stage_payload.get("routes", {})
    alignments_by_id: dict[str, dict[str, Any]] = stage_payload.get(
        "alignments", {}
    )
    cores_by_id: dict[str, dict[str, Any]] = stage_payload.get("cores", {})
    checkpoints: list[dict[str, Any]] = []
    if resumed_from is not None:
        checkpoints.append(resumed_from)
    affected_objects = [workspace.query_by_id[qid] for qid in affected]
    parallel_queries = 0

    def checkpoint(stage: str, payload: dict[str, Any], ids: Iterable[str]) -> dict[str, Any]:
        saved = state.save_checkpoint(
            run_key=run_key,
            stage=stage,
            completed_query_ids=ids,
            payload=payload,
        )
        checkpoints.append(_checkpoint_summary(saved))
        if crash_after == stage:
            raise SimulatedCrash(stage)
        return saved

    if start_index <= STAGES.index("ingest"):
        checkpoint(
            "ingest",
            {
                "document_hash": workspace.corpus.content_hash,
                "queries_hash": workspace.queries_content_hash,
                "affected_query_ids": affected,
                "reused_query_ids": reused,
                "decisions": decisions,
            },
            affected,
        )
    if start_index <= STAGES.index("recall"):
        if set(routes_by_id) != set(affected):
            def recall_worker(query: Query) -> tuple[Query, Any]:
                plan = plans[query.query_id]
                return query, _recall_routes(
                    workspace.corpus,
                    query,
                    workspace.config,
                    plan,
                    cache,
                    use_cache=use_cache,
                )

            routes_by_id = _parallel_map(
                affected_objects, recall_worker, workers
            )
            parallel_queries += len(affected_objects)
        guard.check(
            "recall", len(canonical_bytes(routes_by_id))
        )
        checkpoint("recall", {"routes": routes_by_id}, affected)
    if start_index <= STAGES.index("align"):
        if set(alignments_by_id) != set(affected):
            routes_snapshot = {
                query_id: routes_by_id[query_id] for query_id in affected
            }

            def align_worker(query: Query) -> tuple[Query, Any]:
                return query, align_candidates(
                    query, routes_snapshot[query.query_id], workspace.config
                )

            alignments_by_id = _parallel_map(
                affected_objects, align_worker, workers
            )
            parallel_queries += len(affected_objects)
        guard.check(
            "align", len(canonical_bytes(alignments_by_id))
        )
        checkpoint(
            "align",
            {
                "routes": routes_by_id,
                "alignments": alignments_by_id,
            },
            affected,
        )
    if start_index <= STAGES.index("fusion"):
        if set(cores_by_id) != set(affected):
            def fusion_worker(query: Query) -> tuple[Query, Any]:
                query_id = query.query_id
                fusion_key = fusion_cache_key(
                    documents_content_hash=plans[query_id].dependency_hash,
                    query_content_hash=plans[query_id].query_content_hash,
                    config_digest=workspace.config.digest,
                    upstream_node_hash=_upstream_hash(
                        routes_by_id[query_id]
                    ),
                )
                cached_core = (
                    cache.get_fusion(fusion_key) if use_cache else None
                )
                if cached_core is None:
                    fused = finalize_fusion(
                        query,
                        alignments_by_id[query_id],
                        workspace.config,
                    )
                    cached_core = _core_from_fused(
                        routes_by_id[query_id], fused
                    )
                    cache.put_fusion(fusion_key, cached_core)
                return query, cached_core

            cores_by_id = _parallel_map(
                affected_objects, fusion_worker, workers
            )
            parallel_queries += len(affected)
        guard.check(
            "fusion", len(canonical_bytes(cores_by_id))
        )
        checkpoint("fusion", {"cores": cores_by_id}, affected)
    affected_results: dict[str, dict[str, Any]] = {}
    for query_id in affected:
        core = cores_by_id.get(query_id)
        if core is None:
            raise CandidateDrop(
                "fusion checkpoint is missing query core", doc_id=None
            )
        affected_results[query_id] = _record_from_core(
            plans[query_id], workspace.config, core
        )
    if start_index <= STAGES.index("commit"):
        results = {**reused_records, **affected_results}
        if len(results) != len(workspace.queries):
            missing = sorted(
                set(workspace.query_by_id) - set(results)
            )
            raise CandidateDrop(
                "batch cannot assemble all query results",
                doc_id=missing[0] if missing else None,
            )
        records_to_commit = []
        skipped = []
        for query_id in affected:
            result = affected_results[query_id]
            old = committed.get(query_id)
            if old is not None and old["body_hash"] == digest_json(result):
                skipped.append(query_id)
                continue
            plan = plans[query_id]
            records_to_commit.append(
                {
                    "query_id": query_id,
                    "query_content_hash": plan.query_content_hash,
                    "dependency_hash": plan.dependency_hash,
                    "route_keys": plan.route_keys,
                    "config_payload": workspace.config.payload,
                    "config_digest": workspace.config.digest,
                    "envelope": workspace.config.reference(),
                    "result": result,
                }
            )
        checkpoint_version = checkpoints[-1]["version"] if checkpoints else None
        committed_count = state.commit_many(
            records_to_commit, checkpoint_version=checkpoint_version
        )
        commit_payload = {
            "cores": cores_by_id,
            "committed_query_ids": [
                record["query_id"] for record in records_to_commit
            ],
            "skipped_already_committed_query_ids": skipped,
        }
        checkpoint("commit", commit_payload, [query.query_id for query in workspace.queries])
    else:
        committed_count = 0
        skipped = []
    final_results = {**reused_records, **affected_results}
    if latest is not None and latest["stage"] == "commit":
        final_results = reused_records
    records = _output_records(workspace, final_results)
    guard.check("commit", len(canonical_bytes(records)))
    output_hash = _write_jsonl(output_path, records)
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    report = {
        "schema": "fusion_diag.batch_report.v1",
        "mode": mode,
        "run_key": run_key,
        "output_path": str(output_path),
        "output_sha256": output_hash,
        "query_count": len(workspace.queries),
        "recomputed_query_ids": affected,
        "reused_query_ids": reused,
        "recompute_reasons": {
            query_id: decisions[query_id] for query_id in affected
        },
        "reuse_reasons": {
            query_id: decisions[query_id] for query_id in reused
        },
        "committed_record_count": committed_count,
        "skipped_already_committed_query_ids": skipped,
        "resumed_from": resumed_from,
        "checkpoints": checkpoints,
        "cache": cache.to_dict(),
        "resources": guard.to_dict(),
        "parallelism": {
            "worker_count": workers,
            "parallel_query_stage_work": parallel_queries,
        },
        "input": {
            "document_file_sha256": workspace.document_file_hash,
            "query_file_sha256": workspace.query_file_hash,
            "document_content_hash": workspace.corpus.content_hash,
            "queries_content_hash": workspace.queries_content_hash,
            "input_hash": workspace.input_hash,
        },
        "config": workspace.config.reference(),
        "elapsed_ms": round(elapsed_ms, 3),
        "diff_coverage": {
            "status": "pending_verification" if verify_full else "not_requested",
        },
    }
    if verify_full:
        report["diff_coverage"] = _verify_full(
            workspace, root, records
        )
    return report


def query_one(
    workspace: Workspace,
    query_id: str,
    *,
    workdir: str | Path,
    use_cache: bool = True,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if query_id not in workspace.query_by_id:
        raise FormatCorrupt("query_id not found", field="query_id")
    query = workspace.query_by_id[query_id]
    plan = build_query_plan(workspace.corpus, query, workspace.config)
    cache = ContentAddressedCache(Path(workdir) / "cache")
    started = time.perf_counter()
    core, _, _ = _compute_core(
        workspace.corpus,
        plan,
        workspace.config,
        cache,
        use_cache=use_cache,
    )
    result = _record_from_core(plan, workspace.config, core)
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    trace = result["alignment"]["trace"]
    features = {
        "candidate_count": len(result["alignment"]["candidates"]),
        "normalization_calculations": trace["normalization_calculations"],
        "fusion_weight_count": trace["fusion_weight_count"],
    }
    latency = {
        "observed_ms": round(elapsed_ms, 6),
        "features": features,
        "estimate_model": {
            "formula": "0.05 + 0.002*candidate_count + 0.0005*normalization_calculations + 0.01*fusion_weight_count",
            "estimated_ms": round(
                0.05
                + 0.002 * features["candidate_count"]
                + 0.0005 * features["normalization_calculations"]
                + 0.01 * features["fusion_weight_count"],
                6,
            ),
        },
    }
    return result, latency
