"""End-to-end workbench orchestration."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import numpy as np

from .arbitration import arbitrate
from .cache import ContentAddressedCache
from .canonical import (
    canonical_bytes,
    first_array_deviation,
    format_float,
    sha256_bytes,
    sha256_value,
    stable_float_array,
)
from .errors import (
    E_BLOCK_MISMATCH,
    E_INCONSISTENT,
    WorkbenchError,
)
from .formats import Repository, load_repository, parse_tool_result
from .inputs import load_requests, load_schedule
from .invalidation import compare_active_snapshot, compare_plans
from .merge import merge_request
from .planning import (
    Plan,
    build_plan,
    effective_concurrency,
    sorted_requests,
)
from .retrieval import (
    build_index,
    load_corpus,
    load_corpus_documents,
    retrieve,
)
from .scheduler import (
    admit_requests,
    run_tasks_in_chunks,
    set_worker_context,
)


def _content_memory_bytes(entry: dict[str, Any]) -> int:
    content = entry["content"]
    if isinstance(content, str):
        return len(content.encode("utf-8"))
    return len(canonical_bytes(content))


def _request_contexts(requests: list[dict[str, Any]]) -> dict[str, str]:
    contexts: dict[str, str] = {}
    previous_query: dict[str, str] = {}
    for request in sorted_requests(requests):
        contexts[request["unit_id"]] = previous_query.get(
            request["request_id"], ""
        )
        previous_query[request["request_id"]] = request["query_text"]
    return contexts


def _cache_status_for_keys(
    statuses: dict[str, str], keys: list[str]
) -> str:
    if not keys:
        return "not_run"
    relevant = [statuses.get(key, "not_run") for key in keys]
    if "error" in relevant:
        return "error"
    if "not_run" in relevant:
        return "not_run"
    if any(status == "recomputed" for status in relevant):
        return "recomputed"
    return "hit"


def _read_request_results(
    repository: Repository,
    admitted: list[dict[str, Any]],
    plan: Plan,
    cache: ContentAddressedCache,
) -> tuple[
    dict[str, list[dict[str, Any]]],
    dict[str, str],
    dict[str, WorkbenchError],
    dict[str, Any],
]:
    parsed_by_key: dict[str, dict[str, Any]] = {}
    statuses: dict[str, str] = {}
    per_unit: dict[str, list[dict[str, Any]]] = {}
    errors: dict[str, WorkbenchError] = {}
    for item in admitted:
        request = item["request"]
        unit = request["unit_id"]
        read_keys = plan.request_nodes[unit]["read_keys"]
        parsed_results: list[dict[str, Any]] = []
        try:
            for call, read_key in zip(request["tool_calls"], read_keys):
                if read_key in parsed_by_key:
                    statuses.setdefault(read_key, "hit")
                    parsed_results.append(parsed_by_key[read_key])
                    continue
                cached = cache.get(read_key)
                if cached is not None:
                    statuses[read_key] = "hit"
                    parsed_by_key[read_key] = cached
                    parsed_results.append(cached)
                    continue
                parsed = parse_tool_result(
                    repository.entries[call["result_ref"]],
                    repository.snapshot_version,
                    call["result_format_version"],
                )
                statuses[read_key] = "recomputed"
                parsed_by_key[read_key] = parsed
                cache.put(read_key, parsed)
                parsed_results.append(parsed)
            per_unit[unit] = parsed_results
        except WorkbenchError as exc:
            errors[unit] = exc
    return per_unit, statuses, errors, parsed_by_key


def _flatten_numeric(
    artifacts: list[dict[str, Any]],
    precision: dict[str, Any],
) -> tuple[np.ndarray, list[str]]:
    values: list[float] = []
    unit_order: list[str] = []
    for artifact in artifacts:
        unit_order.append(artifact["unit_id"])
        values.extend(float(value) for value in artifact["numeric_values"])
    return stable_float_array(values, precision), unit_order


def _first_byte_deviation(left: bytes, right: bytes) -> dict[str, Any]:
    limit = min(len(left), len(right))
    index = next(
        (position for position in range(limit) if left[position] != right[position]),
        limit,
    )
    return {
        "byte_index": index,
        "left_byte": left[index] if index < len(left) else None,
        "right_byte": right[index] if index < len(right) else None,
        "left_length": len(left),
        "right_length": len(right),
    }


def _verify_block_equivalence(
    block_artifacts: list[dict[str, Any]],
    full_artifacts: list[dict[str, Any]],
    precision: dict[str, Any],
) -> dict[str, Any]:
    block_array, block_units = _flatten_numeric(block_artifacts, precision)
    full_array, full_units = _flatten_numeric(full_artifacts, precision)
    if block_units != full_units:
        raise WorkbenchError(
            E_BLOCK_MISMATCH,
            "block and full request orders differ",
            block_units=block_units,
            full_units=full_units,
        )
    deviation = first_array_deviation(block_array, full_array)
    if deviation is not None:
        raise WorkbenchError(
            E_BLOCK_MISMATCH,
            "block and full numeric arrays differ",
            first_deviation=deviation,
        )
    block_payload = canonical_bytes(block_artifacts)
    full_payload = canonical_bytes(full_artifacts)
    if block_payload != full_payload:
        raise WorkbenchError(
            E_BLOCK_MISMATCH,
            "block and full artifact payloads differ",
            first_deviation=_first_byte_deviation(
                block_payload, full_payload
            ),
        )
    return {
        "status": "identical",
        "block_count": len(block_artifacts),
        "element_count": int(block_array.size),
        "float_dtype": "float64",
        "score_decimals": int(precision.get("score_decimals", 10)),
        "rounding_mode": str(precision.get("rounding_mode", "half_even")),
        "subnormal_protection": bool(
            precision.get("subnormal_protection", True)
        ),
        "numeric_sha256": sha256_bytes(block_array.tobytes(order="C")),
        "payload_sha256": sha256_bytes(block_payload),
    }


def verify_block_equivalence(
    block_artifacts: list[dict[str, Any]],
    full_artifacts: list[dict[str, Any]],
    precision: dict[str, Any],
) -> dict[str, Any]:
    """Public block/full equivalence gate used by external validators."""

    return _verify_block_equivalence(
        block_artifacts, full_artifacts, precision
    )


def _make_full_artifacts(
    eligible: list[dict[str, Any]],
    parsed_by_unit: dict[str, list[dict[str, Any]]],
    plan: Plan,
    corpus: dict[str, dict[str, Any]],
    index: dict[str, Any],
    config: dict[str, Any],
    contexts: dict[str, str],
) -> list[dict[str, Any]]:
    artifacts = []
    for item in eligible:
        request = item["request"]
        unit = request["unit_id"]
        nodes = plan.request_nodes[unit]
        retrieval_artifact = retrieve(
            request["query_text"],
            config,
            index,
            contexts.get(unit, ""),
        )
        artifact = merge_request(
            request,
            parsed_by_unit[unit],
            retrieval_artifact,
            corpus,
            config,
            nodes["merge_key"],
            nodes["retrieve_key"],
            nodes["read_keys"],
        )
        artifacts.append(artifact)
    return artifacts


def _build_graph_report(
    plan: Plan,
    node_status: dict[str, str],
) -> dict[str, Any]:
    nodes = []
    for node_id in plan.order:
        node = plan.nodes[node_id]
        row = node.to_dict()
        row["cache_status"] = node_status.get(node_id, "not_run")
        nodes.append(row)
    return {
        "nodes": nodes,
        "edges": plan.edges(),
        "topological_order": list(plan.order),
        "node_count": len(nodes),
        "edge_count": len(plan.edges()),
    }


def _status_summary(requests_report: list[dict[str, Any]]) -> dict[str, Any]:
    business_codes: dict[str, int] = {}
    cache_hits = 0
    for request in requests_report:
        code = request["business_code"]
        business_codes[code] = business_codes.get(code, 0) + 1
        if request.get("cache", {}).get("hit"):
            cache_hits += 1
    return {
        "requests_total": len(requests_report),
        "requests_ok": sum(
            1 for request in requests_report if request["status"] == "ok"
        ),
        "requests_error": sum(
            1 for request in requests_report if request["status"] == "error"
        ),
        "business_codes": dict(sorted(business_codes.items())),
        "cache_hit_requests": cache_hits,
        "cache_recomputed_requests": len(requests_report) - cache_hits,
    }


def run_workbench(
    repository_path: str | Path,
    requests_path: str | Path,
    schedule_path: str | Path,
    corpus_path: str | Path | None,
    cache_dir: str | Path,
    output_json: str | Path | None = None,
    output_html: str | Path | None = None,
    baseline_repository_path: str | Path | None = None,
    baseline_schedule_path: str | Path | None = None,
) -> dict[str, Any]:
    repository = load_repository(repository_path)
    requests = load_requests(requests_path)
    base_config = load_schedule(schedule_path)
    if corpus_path is not None and Path(corpus_path).exists():
        documents = load_corpus(corpus_path)
    elif isinstance(repository.raw.get("evidence_corpus"), list):
        documents = load_corpus_documents(
            repository.raw["evidence_corpus"]
        )
    else:
        documents = [
            {
                "doc_id": f"result:{result_ref}",
                "text": (
                    repository.entries[result_ref]["content"]
                    if isinstance(
                        repository.entries[result_ref]["content"], str
                    )
                    else canonical_bytes(
                        repository.entries[result_ref]["content"]
                    ).decode("utf-8")
                ),
                "tags": [],
            }
            for result_ref in sorted(repository.entries)
        ]
        if not documents:
            raise WorkbenchError(
                E_INCONSISTENT,
                "evidence corpus cannot be derived from inputs",
            )
    cache = ContentAddressedCache(cache_dir)
    previous_active = cache.active_snapshot()
    previous_snapshot_id = (
        previous_active["snapshot_id"] if previous_active else None
    )

    config, arbitration_log = arbitrate(repository, requests, base_config)
    plan = build_plan(repository, documents, requests, config)
    invalidation = None
    if baseline_repository_path is not None:
        baseline_repository = load_repository(baseline_repository_path)
        baseline_schedule = load_schedule(
            baseline_schedule_path or schedule_path
        )
        baseline_config, _ = arbitrate(
            baseline_repository, requests, baseline_schedule
        )
        baseline_plan = build_plan(
            baseline_repository, documents, requests, baseline_config
        )
        invalidation = compare_plans(
            baseline_plan,
            plan,
            cache,
            baseline_repository,
            repository,
        )
    elif previous_active is not None:
        invalidation = compare_active_snapshot(
            previous_active["manifest"],
            plan,
            cache,
            repository,
        )

    index_key = plan.nodes["index:evidence"].key
    index_payload = cache.get(index_key)
    index_status = "hit"
    if index_payload is None:
        index_payload = build_index(documents, config)
        cache.put(index_key, index_payload)
        index_status = "recomputed"
    index = index_payload
    corpus = {document["doc_id"]: document for document in documents}
    contexts = _request_contexts(requests)

    admission = admit_requests(repository, requests, plan, config)
    admitted = admission["admitted"]
    (
        parsed_by_unit,
        read_statuses,
        read_errors,
        _parsed_by_key,
    ) = _read_request_results(repository, admitted, plan, cache)

    statuses: dict[str, str] = dict(read_statuses)
    statuses[index_key] = index_status
    artifacts: dict[str, dict[str, Any]] = {}
    retrieval_artifacts: dict[str, dict[str, Any]] = {}
    request_errors: dict[str, WorkbenchError] = dict(read_errors)
    tasks: dict[str, dict[str, Any]] = {}
    eligible: list[dict[str, Any]] = []
    set_worker_context(
        {
            "config": config,
            "index": index,
            "corpus": corpus,
        }
    )
    for item in admitted:
        request = item["request"]
        unit = request["unit_id"]
        if unit in request_errors:
            continue
        nodes = plan.request_nodes[unit]
        merge_key = nodes["merge_key"]
        retrieval_key = nodes["retrieve_key"]
        merge_cached = cache.get(merge_key)
        if merge_cached is not None:
            statuses[merge_key] = "hit"
            artifacts[unit] = merge_cached
            if cache.has(retrieval_key):
                statuses[retrieval_key] = "hit"
            else:
                statuses[retrieval_key] = "reused"
            eligible.append(item)
            continue
        retrieval_artifact = cache.get(retrieval_key)
        if retrieval_artifact is not None:
            statuses[retrieval_key] = "hit"
            retrieval_artifacts[unit] = retrieval_artifact
        else:
            statuses[retrieval_key] = "recomputed"
        tasks[unit] = {
            "request": request,
            "parsed_results": parsed_by_unit[unit],
            "retrieval": retrieval_artifact,
            "retrieval_key": retrieval_key,
            "merge_key": merge_key,
            "read_keys": nodes["read_keys"],
            "context_text": contexts.get(unit, ""),
        }
        eligible.append(item)

    task_results, executed_chunks = run_tasks_in_chunks(
        tasks,
        admission["chunks"],
        effective_concurrency(config),
    )
    for unit, result in task_results.items():
        if not result["ok"]:
            error = result["error"]
            request_errors[unit] = WorkbenchError(
                error["code"],
                error["message"],
                **error.get("details", {}),
            )
            nodes = plan.request_nodes[unit]
            if statuses.get(nodes["retrieve_key"]) == "recomputed":
                statuses[nodes["retrieve_key"]] = "error"
            statuses[nodes["merge_key"]] = "error"
            continue
        nodes = plan.request_nodes[unit]
        retrieval_artifact = result["retrieval"]
        merge_artifact = result["merge"]
        retrieval_artifacts[unit] = retrieval_artifact
        if result["retrieval_computed"]:
            cache.put(nodes["retrieve_key"], retrieval_artifact)
            statuses[nodes["retrieve_key"]] = "recomputed"
        else:
            statuses[nodes["retrieve_key"]] = "hit"
        cache.put(nodes["merge_key"], merge_artifact)
        statuses[nodes["merge_key"]] = "recomputed"
        artifacts[unit] = merge_artifact

    successful_eligible = [
        item
        for item in eligible
        if item["request"]["unit_id"] not in request_errors
        and item["request"]["unit_id"] in artifacts
    ]
    full_artifacts = _make_full_artifacts(
        successful_eligible,
        parsed_by_unit,
        plan,
        corpus,
        index,
        config,
        contexts,
    )
    block_artifacts = [
        artifacts[item["request"]["unit_id"]]
        for item in successful_eligible
    ]
    block_equivalence = _verify_block_equivalence(
        block_artifacts, full_artifacts, config["precision"]
    )

    decision_by_unit = {
        decision["unit_id"]: decision
        for decision in admission["decisions"]
    }
    request_reports: list[dict[str, Any]] = []
    node_status: dict[str, str] = {"index:evidence": index_status}
    for request in sorted_requests(requests):
        unit = request["unit_id"]
        decision = decision_by_unit[unit]
        nodes = plan.request_nodes[unit]
        read_keys = nodes["read_keys"]
        if unit in request_errors:
            error = request_errors[unit]
            code = error.code
            status = "error"
            error_payload = error.to_dict()
            request_nodes_status = "error"
            cache_report = {
                "hit": False,
                "read": _cache_status_for_keys(statuses, read_keys),
                "retrieval": statuses.get(nodes["retrieve_key"], "not_run"),
                "merge": statuses.get(nodes["merge_key"], "not_run"),
            }
        elif not decision["admitted"]:
            code = decision["business_code"]
            status = "error"
            error_payload = {
                "code": code,
                "message": decision["reason"],
                "details": decision.get("precheck_error")
                or decision["violations"],
            }
            request_nodes_status = "not_run"
            cache_report = {
                "hit": False,
                "read": "not_run",
                "retrieval": "not_run",
                "merge": "not_run",
            }
        else:
            code = "E_OK"
            status = "ok"
            error_payload = None
            request_nodes_status = "computed"
            cache_report = {
                "hit": (
                    _cache_status_for_keys(statuses, read_keys) == "hit"
                    and statuses.get(nodes["retrieve_key"]) == "hit"
                    and statuses.get(nodes["merge_key"]) == "hit"
                ),
                "read": _cache_status_for_keys(statuses, read_keys),
                "retrieval": statuses.get(nodes["retrieve_key"], "not_run"),
                "merge": statuses.get(nodes["merge_key"], "not_run"),
            }
        if request_nodes_status != "not_run":
            for read_index, key in enumerate(read_keys):
                node_status[f"read:{unit}:{read_index}"] = statuses.get(
                    key, "not_run"
                )
            node_status[nodes["retrieve"]] = statuses.get(
                nodes["retrieve_key"], "not_run"
            )
            node_status[nodes["merge"]] = statuses.get(
                nodes["merge_key"], "not_run"
            )
        else:
            for index_key_node in nodes["reads"]:
                node_status[index_key_node] = "not_run"
            node_status[nodes["retrieve"]] = "not_run"
            node_status[nodes["merge"]] = "not_run"
        request_report = {
            "unit_id": unit,
            "request_id": request["request_id"],
            "turn_no": request["turn_no"],
            "query_text": request["query_text"],
            "status": status,
            "business_code": code,
            "error": error_payload,
            "cache": cache_report,
            "snapshot_version": repository.snapshot_version,
            "input": deepcopy(request),
            "admission": decision,
        }
        if status == "ok":
            artifact = artifacts[unit]
            request_report["retrieval"] = retrieval_artifacts.get(unit)
            request_report["fragments"] = artifact["fragments"]
            request_report["answer"] = artifact["answer"]
        else:
            request_report["retrieval"] = None
            request_report["fragments"] = []
            request_report["answer"] = None
        request_reports.append(request_report)

    graph = _build_graph_report(plan, node_status)
    summary = _status_summary(request_reports)
    summary.update(
        {
            "selected_profile": config.get("selected_profile", {}).get(
                "name"
            ),
            "precision_mode": config["precision"].get("mode"),
            "repository_snapshot_version": repository.snapshot_version,
            "corpus_hash": index["corpus_hash"],
            "index_hash": index["index_hash"],
            "schedule_hash": sha256_value(config),
        }
    )

    snapshot_manifest = {
        "schema": "ragdiag-run-manifest/v1",
        "repository": repository.canonical_index(),
        "repository_index_hash": repository.index_hash(),
        "schedule_hash": sha256_value(config),
        "selected_profile": config.get("selected_profile"),
        "nodes": {
            node_id: {
                "stage": plan.nodes[node_id].stage,
                "owner": plan.nodes[node_id].owner,
                "key": plan.nodes[node_id].key,
            }
            for node_id in plan.order
        },
        "request_statuses": {
            request["unit_id"]: {
                "status": request["status"],
                "business_code": request["business_code"],
                "answer_digest": (
                    request["answer"]["digest"]
                    if request["answer"] is not None
                    else None
                ),
            }
            for request in request_reports
        },
        "block_equivalence": {
            "status": block_equivalence["status"],
            "numeric_sha256": block_equivalence["numeric_sha256"],
        },
        "invalidation_counts": (
            invalidation["counts"] if invalidation is not None else None
        ),
    }
    snapshot_record = cache.create_snapshot(
        snapshot_manifest,
        previous_snapshot_id,
        activate=False,
    )
    report = {
        "schema": "ragdiag-report/v1",
        "business_code": "E_OK",
        "summary": summary,
        "inputs": {
            "seed": repository.seed,
            "repository_snapshot_version": repository.snapshot_version,
            "repository_index_hash": repository.index_hash(),
            "corpus_hash": index["corpus_hash"],
            "index_hash": index["index_hash"],
            "schedule_hash": sha256_value(config),
            "schedule_config": config,
        },
        "graph": graph,
        "requests": request_reports,
        "budget": {
            "limits": admission["limits"],
            "decisions": admission["decisions"],
            "declared_chunks": admission["chunks"],
            "executed_compute_chunks": executed_chunks,
            "logical_time_used_ms": admission["logical_time_used_ms"],
            "resident_bytes_final": admission["resident_bytes_final"],
        },
        "execution": {
            "engine": "multiprocessing.ProcessPoolExecutor",
            "workers": admission["limits"]["effective_concurrency"],
            "declared_chunk_count": len(admission["chunks"]),
            "computed_task_count": sum(
                len(chunk) for chunk in executed_chunks
            ),
        },
        "arbitration": arbitration_log,
        "block_equivalence": block_equivalence,
        "cache": cache.stats(),
        "invalidation": invalidation,
        "snapshot": {
            "snapshot_id": snapshot_record["snapshot_id"],
            "previous_snapshot_id": previous_snapshot_id,
            "base_snapshot_id": snapshot_record["base_snapshot_id"],
            "manifest_hash": snapshot_record["manifest_hash"],
            "active": True,
            "repository_snapshot_version": repository.snapshot_version,
        },
    }
    from .report import validate_report

    validate_report(report, cache=None)
    cache.activate_snapshot(snapshot_record["snapshot_id"])
    try:
        validate_report(report, cache=cache)
    except Exception:
        if previous_snapshot_id is not None:
            cache.activate_snapshot(previous_snapshot_id)
        elif cache.active_path.exists():
            cache.active_path.unlink()
        raise
    report["cache"] = cache.stats()
    if output_json is not None or output_html is not None:
        from .report import write_report_files

        write_report_files(report, output_json, output_html)
    return report

