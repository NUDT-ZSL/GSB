"""Five-stage orchestrator with snapshot resume and deterministic outputs."""

from __future__ import annotations

import copy
import os
import signal
from pathlib import Path
from typing import Any

from .adaptive import choose_path
from .budget import plan_admission
from .comparison import compare_paths, first_path_deviation
from .config import input_file_hashes, load_config
from .disambiguation import disambiguate_documents
from .errors import E_INDEX_INCONSISTENT, E_PATH_DIVERGENCE, PipelineError
from .extraction import extract_documents
from .features import load_context_features
from .glossary import glossary_index, load_glossary
from .indexer import build_index
from .ingest import load_documents
from .ledger import ledger_checksum, write_ledger
from .rules import load_rules
from .scoring import score_documents
from .snapshots import SnapshotStore, snapshot_metadata
from .util import checksum_object, read_json, write_json_atomic
from .verification import verify_artifacts


ENGINE_VERSION = "term-pipeline-v1"


def _base_fingerprint(config: dict[str, Any], hashes: dict[str, str]) -> str:
    return checksum_object(
        {
            "engine": ENGINE_VERSION,
            "normalization": config["normalization"],
            "glossary_version": config.get("glossary_version"),
            "context_version": config.get("context_version"),
            "scoring": config["scoring"],
            "adaptive": config["adaptive"],
            "budget": config["budget"],
            "inputs": hashes,
        }
    )


def _rule_fingerprint(
    config: dict[str, Any],
    rules: dict[str, Any],
) -> str:
    return checksum_object(
        {
            "rule_fingerprint": rules["fingerprint"],
            "rule_version": rules["rule_version"],
            "disambiguation": config["disambiguation"],
        }
    )


def _crash_if_requested(stage_number: int | None, expected: int) -> None:
    if stage_number == expected:
        os.kill(os.getpid(), signal.SIGKILL)


def _candidate_counts(scored_documents: list[dict[str, Any]]) -> dict[str, int]:
    return {
        item["doc_id"]: len(item["candidates"]) for item in scored_documents
    }


def _is_polysemous_document(
    doc_id: str,
    scored_documents: list[dict[str, Any]],
    glossary: dict[str, Any],
) -> bool:
    senses_by_term, _ = glossary_index(glossary)
    scored_by_doc = {
        item["doc_id"]: item["candidates"] for item in scored_documents
    }
    for candidate in scored_by_doc.get(doc_id, []):
        if len(senses_by_term[candidate["term_id"]]) > 1:
            return True
    return False


def _reuse_monosemous_documents(
    stale_snapshot: dict[str, Any],
    documents: list[dict[str, Any]],
    scored_documents: list[dict[str, Any]],
    glossary: dict[str, Any],
    rules: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    old_docs = {
        item["doc_id"]: item for item in stale_snapshot["payload"]["documents"]
    }
    current_by_doc = {
        item["doc_id"]: item["candidates"] for item in scored_documents
    }
    reused: list[dict[str, Any]] = []
    reused_ids: list[str] = []
    invalidated_ids: list[str] = []
    for doc in documents:
        doc_id = doc["doc_id"]
        if _is_polysemous_document(doc_id, scored_documents, glossary):
            invalidated_ids.append(doc_id)
            continue
        old = old_docs.get(doc_id)
        if old is None:
            invalidated_ids.append(doc_id)
            continue
        old_candidate_ids = sorted(
            result["candidate_id"] for result in old["results"]
        )
        new_candidate_ids = sorted(
            candidate["candidate_id"]
            for candidate in current_by_doc.get(doc_id, [])
        )
        if old_candidate_ids != new_candidate_ids:
            invalidated_ids.append(doc_id)
            continue
        copied = copy.deepcopy(old)
        copied["selected_rule_version"] = rules["rule_version"]
        for result in copied["results"]:
            result["selected_rule_version"] = rules["rule_version"]
        reused.append(copied)
        reused_ids.append(doc_id)
    return reused, reused_ids, invalidated_ids


def _build_diagnostics(
    config: dict[str, Any],
    documents: list[dict[str, Any]],
    glossary: dict[str, Any],
    features: dict[str, Any],
    rules: dict[str, Any],
    base_fingerprint: str,
    rule_fingerprint: str,
    path_decision: dict[str, Any],
    budget_plan: dict[str, Any],
    invalidation: dict[str, Any],
    snapshots: list[dict[str, Any]],
    export_payload: dict[str, Any],
    comparison_summary: dict[str, Any],
) -> dict[str, Any]:
    return {
        "run": {
            "config_version": config.get("config_version"),
            "seed": config.get("seed"),
            "engine_version": ENGINE_VERSION,
        },
        "inputs": {
            "doc_count": len(documents),
            "shard_count": len({doc["shard_id"] for doc in documents}),
            "glossary_version": glossary["glossary_version"],
            "glossary_entry_count": len(glossary["entries"]),
            "context_version": features["context_version"],
            "context_observation_count": len(features["observations"]),
            "rule_version": rules["rule_version"],
            "rule_count": len(rules["rules"]),
        },
        "fingerprints": {
            "base": base_fingerprint,
            "rules": rule_fingerprint,
        },
        "adaptive_decision": path_decision,
        "budget_plan": budget_plan,
        "rule_invalidation": invalidation,
        "stages": [snapshot_metadata(snapshot) for snapshot in snapshots],
        "export": export_payload,
        "path_comparison": comparison_summary,
    }


def run_pipeline(
    config_path: str | Path,
    *,
    crash_after_stage: int | None = None,
) -> dict[str, Any]:
    if crash_after_stage is not None and not 1 <= crash_after_stage <= 5:
        raise ValueError("crash_after_stage must be between 1 and 5")
    config = load_config(config_path)
    out_dir = Path(config["_resolved_paths"]["out_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    store = SnapshotStore(config["_resolved_paths"]["snapshot_dir"])
    snapshots = store.load_all()

    hashes = input_file_hashes(config)
    base_fingerprint = _base_fingerprint(config, hashes)
    glossary = load_glossary(config)

    if snapshots and snapshots[0]["base_fingerprint"] != base_fingerprint:
        store.clear_all()
        snapshots = []

    if snapshots:
        documents = snapshots[0]["payload"]["documents"]
    else:
        documents = load_documents(config)
    document_ids = [doc["doc_id"] for doc in documents]
    features = load_context_features(config, documents)
    rules = load_rules(config, glossary, features)
    rule_fingerprint = _rule_fingerprint(config, rules)

    stale_disambiguation = None
    valid_count = 0
    for snapshot in snapshots:
        if snapshot["base_fingerprint"] != base_fingerprint:
            break
        if (
            snapshot["stage_index"] >= 3
            and snapshot.get("rule_fingerprint") != rule_fingerprint
        ):
            if snapshot["stage_index"] == 3:
                stale_disambiguation = snapshot
            break
        valid_count += 1
    if valid_count < len(snapshots):
        store.truncated(valid_count)
        snapshots = snapshots[:valid_count]

    if not snapshots:
        payload = {"documents": documents}
        snapshots.append(
            store.commit(
                0,
                payload,
                document_ids,
                base_fingerprint=base_fingerprint,
                expected_doc_ids=document_ids,
            )
        )
    _crash_if_requested(crash_after_stage, 1)

    if len(snapshots) < 2:
        extracted = extract_documents(documents, glossary, config)
        payload = {"documents": extracted}
        snapshots.append(
            store.commit(
                1,
                payload,
                document_ids,
                base_fingerprint=base_fingerprint,
                expected_doc_ids=document_ids,
            )
        )
    else:
        extracted = snapshots[1]["payload"]["documents"]
    _crash_if_requested(crash_after_stage, 2)

    if len(snapshots) < 3:
        scored_documents = score_documents(
            documents,
            extracted,
            glossary,
            features,
            config,
        )
        path_decision = choose_path(
            documents,
            scored_documents,
            _candidate_counts(scored_documents),
            features,
            config,
        )
        budget_plan = plan_admission(
            documents,
            scored_documents,
            features,
            config,
        )
        payload = {
            "documents": scored_documents,
            "adaptive_decision": path_decision,
            "budget_plan": budget_plan,
        }
        snapshots.append(
            store.commit(
                2,
                payload,
                document_ids,
                base_fingerprint=base_fingerprint,
                expected_doc_ids=document_ids,
            )
        )
    else:
        scored_documents = snapshots[2]["payload"]["documents"]
        path_decision = snapshots[2]["payload"]["adaptive_decision"]
        budget_plan = snapshots[2]["payload"]["budget_plan"]
    _crash_if_requested(crash_after_stage, 3)

    invalidation: dict[str, Any] = {}
    if len(snapshots) < 4:
        if stale_disambiguation is not None:
            reused, reused_ids, invalidated_ids = _reuse_monosemous_documents(
                stale_disambiguation,
                documents,
                scored_documents,
                glossary,
                rules,
            )
            reused_by_doc = {item["doc_id"]: item for item in reused}
            current_polysemous = disambiguate_documents(
                [
                    doc
                    for doc in documents
                    if doc["doc_id"] in set(invalidated_ids)
                ],
                scored_documents,
                glossary,
                rules,
                features,
                path_decision,
                config,
            )
            by_doc = {
                item["doc_id"]: item for item in current_polysemous
            }
            merged = []
            for doc in documents:
                doc_id = doc["doc_id"]
                if doc_id in by_doc:
                    merged.append(by_doc[doc_id])
                else:
                    merged.append(reused_by_doc[doc_id])
            disambiguated = merged
            invalidation = {
                "scope": "targeted_documents",
                "reason": "RULE_SNAPSHOT_CHANGED",
                "old_rule_fingerprint": stale_disambiguation.get(
                    "rule_fingerprint"
                ),
                "new_rule_fingerprint": rule_fingerprint,
                "invalidated_doc_ids": sorted(invalidated_ids),
                "reused_doc_ids": sorted(reused_ids),
                "invalidated_doc_count": len(invalidated_ids),
                "reused_doc_count": len(reused_ids),
            }
        else:
            disambiguated = disambiguate_documents(
                documents,
                scored_documents,
                glossary,
                rules,
                features,
                path_decision,
                config,
            )
            invalidation = {
                "scope": "full_rebuild",
                "reason": "NO_COMPATIBLE_DISAMBIGUATION_SNAPSHOT",
                "old_rule_fingerprint": None,
                "new_rule_fingerprint": rule_fingerprint,
                "invalidated_doc_ids": sorted(document_ids),
                "reused_doc_ids": [],
                "invalidated_doc_count": len(document_ids),
                "reused_doc_count": 0,
            }
        payload = {
            "documents": disambiguated,
            "selected_rule_version": rules["rule_version"],
            "rule_invalidation": invalidation,
        }
        snapshots.append(
            store.commit(
                3,
                payload,
                document_ids,
                base_fingerprint=base_fingerprint,
                rule_fingerprint=rule_fingerprint,
                invalidation=invalidation,
                expected_doc_ids=document_ids,
            )
        )
    else:
        disambiguated = snapshots[3]["payload"]["documents"]
        invalidation = snapshots[3]["payload"].get("rule_invalidation", {})
    _crash_if_requested(crash_after_stage, 4)

    index_path = out_dir / "term_index.json"
    ledger_path = out_dir / "ledger.sqlite3"
    comparison_path = out_dir / "path_comparison.json"
    diagnostics_path = out_dir / "diagnostics.json"
    artifacts_exist = (
        index_path.exists() and ledger_path.exists() and comparison_path.exists()
    )

    if len(snapshots) >= 5 and not artifacts_exist:
        store.truncated(4)
        snapshots = snapshots[:4]

    if len(snapshots) < 5:
        comparison = compare_paths(
            documents,
            scored_documents,
            glossary,
            rules,
            features,
            path_decision,
            config,
            strict=False,
        )
        if not comparison["index_stats_comparable"]:
            raise PipelineError(
                E_PATH_DIVERGENCE,
                "exact and restricted-approximate paths produced incomparable index statistics",
                {
                    "difference_classes": comparison["difference_classes"],
                    "first_deviation": first_path_deviation(comparison),
                    "coverage_recall_estimate": comparison[
                        "coverage_recall_estimate"
                    ],
                    "term_set_diff": comparison["term_set_diff"],
                    "sense_set_diff": comparison["sense_set_diff"],
                    "report": comparison,
                },
            )
        index = build_index(
            disambiguated,
            config,
            selected_path=path_decision["selected_path"],
            rule_version=rules["rule_version"],
        )
        write_json_atomic(index_path, index)
        write_json_atomic(comparison_path, comparison)
        ledger_info = write_ledger(
            ledger_path,
            documents,
            scored_documents,
            disambiguated,
            features,
            path_decision,
            budget_plan,
            config,
        )
        selfcheck = verify_artifacts(
            index,
            ledger_path,
            documents,
            scored_documents,
            disambiguated,
            config,
        )
        export_payload = {
            "index_file": index_path.name,
            "ledger_file": ledger_path.name,
            "comparison_file": comparison_path.name,
            "diagnostics_file": diagnostics_path.name,
            "index_checksum": index["checksum"],
            "ledger_checksum": ledger_info["checksum"],
            "comparison_checksum": checksum_object(comparison),
            "selfcheck": selfcheck,
        }
        snapshots.append(
            store.commit(
                4,
                export_payload,
                document_ids,
                base_fingerprint=base_fingerprint,
                rule_fingerprint=rule_fingerprint,
                invalidation=invalidation,
                expected_doc_ids=document_ids,
            )
        )
        _crash_if_requested(crash_after_stage, 5)
    else:
        export_payload = snapshots[4]["payload"]
        index = read_json(index_path)
        comparison = read_json(comparison_path)
        if checksum_object(comparison) != export_payload.get(
            "comparison_checksum"
        ):
            raise PipelineError(
                E_INDEX_INCONSISTENT,
                "path comparison artifact checksum changed",
                {
                    "first_deviation": {
                        "type": "artifact_checksum_mismatch",
                        "file": comparison_path.name,
                        "recorded": export_payload.get(
                            "comparison_checksum"
                        ),
                        "actual": checksum_object(comparison),
                    }
                },
            )
        if index.get("checksum") != export_payload.get("index_checksum"):
            raise PipelineError(
                E_INDEX_INCONSISTENT,
                "term index artifact checksum changed",
                {
                    "first_deviation": {
                        "type": "artifact_checksum_mismatch",
                        "file": index_path.name,
                        "recorded": export_payload.get("index_checksum"),
                        "actual": index.get("checksum"),
                    }
                },
            )
        ledger_info = ledger_checksum(ledger_path)
        if ledger_info["checksum"] != export_payload.get("ledger_checksum"):
            raise PipelineError(
                E_INDEX_INCONSISTENT,
                "ledger artifact checksum changed",
                {
                    "first_deviation": {
                        "type": "artifact_checksum_mismatch",
                        "file": ledger_path.name,
                        "recorded": export_payload.get("ledger_checksum"),
                        "actual": ledger_info["checksum"],
                    }
                },
            )
        selfcheck = verify_artifacts(
            index,
            ledger_path,
            documents,
            scored_documents,
            disambiguated,
            config,
        )

    diagnostics = _build_diagnostics(
        config,
        documents,
        glossary,
        features,
        rules,
        base_fingerprint,
        rule_fingerprint,
        path_decision,
        budget_plan,
        invalidation,
        snapshots,
        export_payload,
        {
            "adjudication": comparison["adjudication"],
            "index_stats_comparable": comparison["index_stats_comparable"],
            "coverage_recall_estimate": comparison["coverage_recall_estimate"],
            "difference_classes": comparison["difference_classes"],
            "report_checksum": checksum_object(comparison),
        },
    )
    write_json_atomic(diagnostics_path, diagnostics)
    return {
        "ok": True,
        "doc_count": len(documents),
        "candidate_count": sum(
            len(doc["candidates"]) for doc in scored_documents
        ),
        "index_entry_count": index["entry_count"],
        "index_occurrence_count": index["occurrence_count"],
        "selected_path": path_decision["selected_path"],
        "snapshot_versions": [
            snapshot["version"] for snapshot in snapshots
        ],
        "outputs": {
            "index": str(index_path),
            "ledger": str(ledger_path),
            "diagnostics": str(diagnostics_path),
            "comparison": str(comparison_path),
        },
        "selfcheck": selfcheck,
    }

