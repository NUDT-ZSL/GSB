from __future__ import annotations

import json
import platform
import shutil
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

import numpy as np

from .arbitration import arbitrate
from .config import PipelineConfig
from .errors import AppError
from .formats import parse_answer, serialize
from .generation import load_documents, load_queries, write_samples
from .migration import migrate_answer
from .parallel import run_batch
from .pipeline import IncrementalEngine, Pipeline, PipelineResult
from .selfcheck import consistency_check
from .snapshot import VersionedSnapshotStore
from .util import (
    atomic_write_text,
    canonical_json,
    canonical_json_pretty,
    sha256_text,
)


def _capture(probe: str, action: Callable[[], Any]) -> dict[str, Any]:
    try:
        action()
    except AppError as exc:
        return {
            "probe": probe,
            "expected": True,
            "code": exc.code,
            "message": exc.message,
            "details": exc.details,
        }
    return {
        "probe": probe,
        "expected": True,
        "code": "E_EXPECTED_ERROR_NOT_RAISED",
        "message": "fault probe did not raise the expected business error",
        "details": None,
    }


def _roundtrip(package: Any, documents: dict[str, Any]) -> dict[str, Any]:
    report: dict[str, Any] = {}
    for version in (1, 2, 3):
        text = serialize(package, version)
        parsed = parse_answer(text, documents, expected_format=version)
        text_again = serialize(parsed.package, version)
        source_keys = [(item.doc_id, item.start, item.end) for item in package.snippets]
        parsed_keys = [(item.doc_id, item.start, item.end) for item in parsed.package.snippets]
        report[f"v{version}"] = {
            "ok": text == text_again and source_keys == parsed_keys,
            "source_sha256": sha256_text(text),
            "roundtrip_sha256": sha256_text(text_again),
            "byte_stable": text == text_again,
            "spans_stable": source_keys == parsed_keys,
            "span_count": len(source_keys),
        }
    return report


def _snapshot_payloads(
    result: PipelineResult,
    documents: dict[str, Any],
) -> dict[str, Any]:
    return {
        "doc_version_table": {
            "documents": {
                doc_id: document.doc_version
                for doc_id, document in sorted(documents.items())
            },
            "citation_doc_versions": dict(sorted(result.package.doc_versions.items())),
        },
        "citations_snapshot": {
            "conv_id": result.package.conv_id,
            "turn_no": result.package.turn_no,
            "citations": [citation.to_dict() for citation in result.package.citations],
        },
        "answer_snapshot": {
            "conv_id": result.package.conv_id,
            "turn_no": result.package.turn_no,
            "answer_text": result.package.answer_text,
            "answer_sha256": result.package.answer_sha256,
            "byte_hash": result.byte_hash,
        },
    }


def _snapshot_probes(root: Path, baseline_payload: dict[str, Any]) -> dict[str, Any]:
    probes: list[dict[str, Any]] = []
    store = VersionedSnapshotStore(root)
    store.replace("probe", "answer", baseline_payload, version=2)
    probes.append(
        _capture(
            "old snapshot version cannot overwrite newer",
            lambda: store.replace("probe", "answer", baseline_payload, version=1),
        )
    )
    corrupt_path = store._snapshot_path("probe", "corrupt")
    store.replace("probe", "corrupt", baseline_payload, version=1)
    corrupt = json.loads(corrupt_path.read_text(encoding="utf-8"))
    corrupt["payload"]["tampered"] = True
    atomic_write_text(corrupt_path, canonical_json(corrupt) + "\n")
    probes.append(
        _capture(
            "snapshot checksum mismatch is rejected",
            lambda: store.load("probe", "corrupt"),
        )
    )

    def busy_write() -> None:
        with store.guard("probe", "busy"):
            store.replace("probe", "busy", baseline_payload, version=1)

    probes.append(
        _capture("concurrent writer gets immediate stage busy", busy_write)
    )
    return {
        "ok": all(item["code"] != "E_EXPECTED_ERROR_NOT_RAISED" for item in probes),
        "probes": probes,
    }


def _business_probe_errors(
    result: PipelineResult,
    documents: dict[str, Any],
    queries: list[Any],
    config: PipelineConfig,
) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    package = result.package
    first_citation = package.citations[0]
    first_snippet = package.snippets[0]
    out_of_range_package = replace(
        package,
        citations=(replace(first_citation, end=len(documents[first_citation.doc_id].text) + 1),),
        snippets=(first_snippet,),
    )
    errors.append(
        _capture(
            "span beyond codepoint length",
            lambda: out_of_range_package.validate(documents),
        )
    )
    duplicate_package = replace(
        package,
        citations=(first_citation, first_citation),
        snippets=(first_snippet, first_snippet),
    )
    errors.append(
        _capture(
            "duplicate citation interval",
            lambda: duplicate_package.validate(documents),
        )
    )
    errors.append(
        _capture(
            "unsupported answer format",
            lambda: parse_answer("CQIR-ANSWER/9\n", documents),
        )
    )
    errors.append(
        _capture(
            "truncated answer format header",
            lambda: parse_answer("CQIR-AN\n", documents),
        )
    )
    rich_package = replace(
        package,
        citations=tuple(replace(citation, revision=4) for citation in package.citations),
    )
    v1_text = serialize(rich_package, 1)
    v3_text = serialize(rich_package, 3)
    errors.append(
        _capture(
            "v1 cannot recover revision semantics",
            lambda: arbitrate(
                {"v1": v1_text, "v3": v3_text},
                documents,
                queries,
                config,
            ),
        )
    )
    bad_stage_sets = {
        name: dict(value)
        for name, value in result.stage_sets.items()
    }
    bad_stage_sets["normalized"]["conv_id"] = "conv-mismatch"
    errors.append(
        _capture(
            "cross-stage conv_id inconsistency",
            lambda: consistency_check(
                package,
                bad_stage_sets,
                {},
            ),
        )
    )
    return errors


def run_demo(
    workspace: Path,
    primary_conv: str = "conv-002",
    primary_turn: int = 1,
    seed: int = 20261122,
) -> dict[str, Any]:
    samples_root = workspace / "samples"
    cache_root = workspace / ".cqir" / "cache"
    replay_root = workspace / ".cqir" / "replay_cache"
    snapshot_root = workspace / ".cqir" / "snapshots"
    samples_info = write_samples(samples_root, seed)
    queries = load_queries(samples_root / "queries.jsonl")
    documents = load_documents(samples_root / "documents.json")
    bumped_documents = load_documents(samples_root / "documents_bumped.json")
    query = next(
        item
        for item in queries
        if item.conv_id == primary_conv and item.turn_no == primary_turn
    )
    config = PipelineConfig(seed=seed)
    if snapshot_root.exists():
        shutil.rmtree(snapshot_root)
    pipeline = Pipeline(config=config, cache_root=cache_root)
    baseline = pipeline.run(query, queries, documents, force_recompute=True)
    selfcheck = consistency_check(baseline.package, baseline.stage_sets)

    formats = {
        "v1": serialize(baseline.package, 1),
        "v2": serialize(baseline.package, 2),
        "v3": serialize(baseline.package, 3),
    }
    cross_format = arbitrate(formats, documents, queries, config)
    roundtrip = _roundtrip(baseline.package, documents)

    migration_matrix: dict[str, Any] = {}
    for source in (1, 2, 3):
        for target in (1, 2, 3):
            if source == target:
                continue
            key = f"v{source}_to_v{target}"
            migrated = migrate_answer(
                serialize(baseline.package, source),
                target,
                documents,
                queries,
                config,
            )
            migration_matrix[key] = {
                "source_version": source,
                "target_version": target,
                "fill_count": len(migrated["fills"]),
                "drop_count": len(migrated["drops"]),
                "fills": migrated["fills"],
                "drops": migrated["drops"],
                "span_check": migrated["span_check"],
            }

    incremental_engine = IncrementalEngine(pipeline)
    incremental = incremental_engine.run(
        query,
        queries,
        bumped_documents,
        previous_result=baseline,
        changed_doc_ids=["doc-002"],
        deleted_doc_ids=["doc-005"],
    )
    incremental_result: PipelineResult = incremental["result"]
    incremental_report = incremental["report"]
    incremental_selfcheck = consistency_check(
        incremental_result.package,
        incremental_result.stage_sets,
        retain_set=[
            [item[0], item[1], item[2]]
            for item in incremental_report["reuse_set"]["citation_keys"]
        ],
        evict_set=[
            [item[0], item[1], item[2]]
            for item in incremental_report["recompute_set"]["citation_keys"]
        ],
    )

    fresh_pipeline = Pipeline(
        config=config,
        cache_root=replay_root,
    )
    fresh_result = fresh_pipeline.run(
        query,
        queries,
        bumped_documents,
        force_recompute=True,
    )
    incremental_formats = {
        "v1": serialize(incremental_result.package, 1),
        "v2": serialize(incremental_result.package, 2),
        "v3": serialize(incremental_result.package, 3),
    }
    same_bytes = (
        incremental_result.canonical_answer.encode("utf-8")
        == fresh_result.canonical_answer.encode("utf-8")
        and incremental_formats["v3"].encode("utf-8")
        == serialize(fresh_result.package, 3).encode("utf-8")
    )

    snapshot_store = VersionedSnapshotStore(snapshot_root)
    baseline_payloads = _snapshot_payloads(baseline, documents)
    bumped_payloads = _snapshot_payloads(incremental_result, bumped_documents)
    snapshot_records: dict[str, Any] = {}
    for name in ("doc_version_table", "citations_snapshot", "answer_snapshot"):
        snapshot_records[name] = {
            "v1": snapshot_store.replace(name, "session", baseline_payloads[name], 1),
            "v2": snapshot_store.replace(
                name,
                "session",
                bumped_payloads[name],
                2,
                expected_version=1,
            ),
        }
    loaded_snapshots = {
        name: snapshot_store.load(name, "session")
        for name in snapshot_records
    }
    snapshot_versions = {
        name: (snapshot["version"] if snapshot else None)
        for name, snapshot in loaded_snapshots.items()
    }
    with tempfile.TemporaryDirectory(prefix="cqir-snapshot-probe-") as temporary:
        snapshot_probe = _snapshot_probes(Path(temporary), baseline_payloads["answer_snapshot"])

    business_errors = _business_probe_errors(
        baseline,
        documents,
        queries,
        config,
    )
    business_errors.extend(snapshot_probe["probes"])

    batch_queries = [item for item in queries if item.conv_id == "conv-001"][:2]
    batch_workers = max(1, min(2, len(batch_queries)))
    parallel_cache = workspace / ".cqir" / "parallel_cache"
    parallel_results = run_batch(
        batch_queries,
        queries,
        documents,
        config,
        parallel_cache,
        workers=batch_workers,
        force_recompute=True,
    )
    sequential_results = run_batch(
        batch_queries,
        queries,
        documents,
        config,
        parallel_cache,
        workers=1,
        force_recompute=True,
    )
    parallel_equal = parallel_results == sequential_results

    checks = {
        "baseline_selfcheck": selfcheck["ok"],
        "cross_format_arbitration": cross_format["ok"],
        "roundtrip_all_formats": all(item["ok"] for item in roundtrip.values()),
        "incremental_selfcheck": incremental_selfcheck["ok"],
        "incremental_equals_fresh": same_bytes,
        "snapshot_versions_monotonic": snapshot_versions
        == {
            "doc_version_table": 2,
            "citations_snapshot": 2,
            "answer_snapshot": 2,
        },
        "snapshot_fault_probes": snapshot_probe["ok"],
        "business_fault_probes": all(
            item["code"] != "E_EXPECTED_ERROR_NOT_RAISED"
            for item in business_errors
        ),
        "parallel_process_pool_equal": parallel_equal,
    }
    result = {
        "meta": {
            "seed": seed,
            "platform": platform.platform(),
            "system": platform.system(),
            "python": platform.python_version(),
            "numpy": np.__version__,
            "wsl": "microsoft" in platform.release().lower(),
            "primary_query": query.to_dict(),
            "samples": samples_info,
            "config": config.to_dict(),
            "config_hash": config.content_hash,
            "deterministic_clock_free": True,
        },
        "checks": checks,
        "all_passed": all(checks.values()),
        "baseline": {
            "package": baseline.package.semantic_dict(),
            "package_storage": baseline.package.to_storage_dict(),
            "normalized": baseline.normalized,
            "recall": baseline.recall,
            "snippet_candidates": baseline.snippet_candidates,
            "selected": baseline.selected,
            "stage_report": baseline.stage_report,
            "stage_sets": baseline.stage_sets,
            "selfcheck": selfcheck,
            "formats": formats,
            "arbitration": cross_format,
            "roundtrip": roundtrip,
            "byte_hash": baseline.byte_hash,
        },
        "migration_matrix": migration_matrix,
        "incremental": {
            "package": incremental_result.package.semantic_dict(),
            "selected": incremental_result.selected,
            "recall": incremental_result.recall,
            "stage_sets": incremental_result.stage_sets,
            "report": incremental_report,
            "selfcheck": incremental_selfcheck,
            "formats": incremental_formats,
            "byte_hash": incremental_result.byte_hash,
            "fresh_byte_hash": fresh_result.byte_hash,
            "fresh_equals_incremental": same_bytes,
            "removed_docs": incremental_report["removed_docs"],
        },
        "snapshots": {
            "versions": snapshot_versions,
            "records": snapshot_records,
            "loaded": loaded_snapshots,
            "probe": snapshot_probe,
        },
        "error_journal": business_errors,
        "parallel": {
            "workers": batch_workers,
            "query_count": len(batch_queries),
            "equal_to_sequential": parallel_equal,
            "results": parallel_results,
        },
    }
    return result


def write_demo_outputs(workspace: Path, result: dict[str, Any]) -> dict[str, str]:
    artifact_root = workspace / "artifacts"
    artifact_root.mkdir(parents=True, exist_ok=True)
    answer_root = artifact_root / "answers"
    persisted_views: dict[str, str] = {}
    for report_name in ("baseline", "incremental"):
        package = result[report_name]["package"]
        conv_id = str(package["conv_id"])
        turn_no = int(package["turn_no"])
        for version in (1, 2, 3):
            path = answer_root / f"{report_name}.{conv_id}.turn-{turn_no}.v{version}.txt"
            atomic_write_text(path, result[report_name]["formats"][f"v{version}"])
            persisted_views[f"{report_name}.v{version}"] = str(path)
    result_path = artifact_root / "demo_result.json"
    atomic_write_text(result_path, canonical_json_pretty(result) + "\n")
    summary = {
        "all_passed": result["all_passed"],
        "checks": result["checks"],
        "baseline_byte_hash": result["baseline"]["byte_hash"],
        "incremental_byte_hash": result["incremental"]["byte_hash"],
        "fresh_byte_hash": result["incremental"]["fresh_byte_hash"],
        "snapshot_versions": result["snapshots"]["versions"],
        "error_codes": sorted(
            {item["code"] for item in result["error_journal"]}
        ),
        "focused_query": result["meta"]["primary_query"],
    }
    summary_path = artifact_root / "demo_summary.json"
    atomic_write_text(summary_path, canonical_json_pretty(summary) + "\n")
    markdown_lines = [
        "# CQIR Demo Verification",
        "",
        f"- All checks passed: `{result['all_passed']}`",
        f"- Baseline byte hash: `{result['baseline']['byte_hash']}`",
        f"- Incremental byte hash: `{result['incremental']['byte_hash']}`",
        f"- Fresh replay byte hash: `{result['incremental']['fresh_byte_hash']}`",
        f"- Incremental equals fresh: `{result['incremental']['fresh_equals_incremental']}`",
        f"- Snapshot versions: `{json.dumps(result['snapshots']['versions'], sort_keys=True)}`",
        f"- Error probes: `{', '.join(sorted({item['code'] for item in result['error_journal']}))}`",
        "",
        "## Checks",
    ]
    for name, value in sorted(result["checks"].items()):
        markdown_lines.append(f"- `{name}`: `{value}`")
    markdown_path = artifact_root / "demo_verification.md"
    atomic_write_text(markdown_path, "\n".join(markdown_lines) + "\n")
    return {
        "result": str(result_path),
        "summary": str(summary_path),
        "markdown": str(markdown_path),
        "answer_views": persisted_views,
    }
