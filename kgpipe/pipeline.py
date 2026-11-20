from __future__ import annotations

from pathlib import Path
from typing import Any

from .adaptive import decide_path, with_overrides
from .config import load_config
from .corpus import load_corpus
from .dictionary import EntityDictionary
from .errors import KGError, error_dict
from .models import CandidateTriple, Corpus, DocumentEntities
from .normalization import normalize_document, recompute_graph_stats
from .paths import compare_candidate_sets
from .recognition import recognize_document
from .relation import RelationSchema, extract_relations
from .snapshot import SnapshotStore
from .util import (
    atomic_write_json,
    atomic_write_text,
    canonical_json,
    content_checksum,
    stable_id,
)
from .verifier import evaluate_truth, load_truth, verify_execution_report


PIPELINE_REPORT_SCHEMA = 1


def _candidate_key(triple: CandidateTriple) -> tuple[Any, ...]:
    return (
        triple.doc_id,
        triple.sent_no,
        triple.head_span_id,
        triple.tail_span_id,
        triple.relation_type,
    )


def _relation_path_rows(
    triples_by_doc: dict[str, list[CandidateTriple]],
) -> dict[str, list[dict[str, Any]]]:
    return {
        doc_id: [triple.to_dict() for triple in triples_by_doc[doc_id]]
        for doc_id in sorted(triples_by_doc)
    }


def _entity_recognition_report(
    corpus: Corpus,
    entities_by_doc: dict[str, DocumentEntities],
    entity_dictionary: EntityDictionary,
) -> dict[str, Any]:
    documents: list[dict[str, Any]] = []
    for document in corpus.documents:
        entities = entities_by_doc[document.doc_id]
        documents.append(
            {
                "doc_id": document.doc_id,
                "shard_ids": list(document.shard_ids),
                "sentences": [
                    {
                        "sent_no": sentence.sent_no,
                        "char_len": sentence.char_len,
                    }
                    for sentence in document.sentences
                ],
                "spans": [span.to_dict() for span in entities.spans],
                "tokens_by_sentence": {
                    str(sent_no): [token.to_dict() for token in tokens]
                    for sent_no, tokens in sorted(
                        entities.sentence_tokens.items()
                    )
                },
            }
        )
    return {
        "dictionary": entity_dictionary.checksum_record(),
        "tokenization": "han_codepoint_and_ascii_word_v1",
        "match_strategy": "global_longest_then_start_then_type_id_v1",
        "documents": documents,
    }


def _cross_path_report(
    *,
    corpus: Corpus,
    exact_by_doc: dict[str, list[CandidateTriple]],
    approx_by_doc: dict[str, list[CandidateTriple]],
    schema: RelationSchema,
) -> dict[str, Any]:
    mandatory_lines = [pattern.line_no for pattern in schema.mandatory_patterns]
    rows = [
        compare_candidate_sets(
            doc_id=document.doc_id,
            exact=exact_by_doc[document.doc_id],
            approximate=approx_by_doc[document.doc_id],
            mandatory_pattern_lines=mandatory_lines,
        )
        for document in corpus.documents
    ]
    total_exact = sum(row["exact_count"] for row in rows)
    total_common = sum(row["common_count"] for row in rows)
    aggregate_recall = 1.0 if total_exact == 0 else total_common / total_exact
    comparable = all(row["statistics_comparable"] for row in rows)
    first_doc = next(
        (row["doc_id"] for row in rows if not row["statistics_comparable"]),
        None,
    )
    first_row = next(
        (row for row in rows if row["doc_id"] == first_doc),
        None,
    )
    difference_causes = sorted(
        {
            cause
            for row in rows
            for cause in row.get("difference_causes", [])
        }
    )
    report: dict[str, Any] = {
        "documents": rows,
        "mandatory_pattern_lines": mandatory_lines,
        "total_exact_triples": total_exact,
        "total_approx_triples": sum(row["approx_count"] for row in rows),
        "total_common_triples": total_common,
        "aggregate_recall_estimate": round(aggregate_recall, 6),
        "statistics_comparable": comparable,
        "difference_causes": difference_causes,
        "first_divergence_doc_id": first_doc,
        "first_divergence_triple": (
            first_row.get("first_divergence") if first_row else None
        ),
        "adjudication": (
            "identical"
            if comparable
            else "canonical_exact_path_retained"
        ),
    }
    if not comparable:
        report["error"] = error_dict(
            "E_PATH_DIVERGENCE",
            "精确路径与近似路径的图谱统计口径不可比",
            category="CROSS_PATH_DIVERGENCE",
            doc_id=first_doc,
            first_divergence_triple=report["first_divergence_triple"],
            difference_causes=difference_causes,
            aggregate_recall_estimate=report["aggregate_recall_estimate"],
        )
    return report


def _mandatory_preserved(
    *,
    exact: list[CandidateTriple],
    approximate: list[CandidateTriple],
    mandatory_lines: set[int],
) -> dict[str, Any]:
    exact_mandatory = {
        _candidate_key(triple): triple
        for triple in exact
        if triple.pattern_line in mandatory_lines
    }
    approx_keys = {_candidate_key(triple) for triple in approximate}
    missing = sorted(set(exact_mandatory) - approx_keys)
    return {
        "required_count": len(exact_mandatory),
        "preserved_count": len(exact_mandatory) - len(missing),
        "preserved": not missing,
        "missing": [
            {
                "triple_id": exact_mandatory[key].triple_id,
                "sent_no": key[1],
                "relation_type": key[4],
            }
            for key in missing
        ],
    }


def _write_log(out_dir: Path, events: list[dict[str, Any]]) -> None:
    lines = [canonical_json(event) for event in events]
    atomic_write_text(out_dir / "pipeline.log", "\n".join(lines) + "\n")


def _base_report(
    *,
    data_dir: str | Path,
    config: dict[str, Any] | None,
    request: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": PIPELINE_REPORT_SCHEMA,
        "status": "error",
        "run_id": None,
        "data_dir": str(Path(data_dir)),
        "request": request,
        "config": config,
        "input": {},
        "selection": {},
        "stages": {},
        "errors": [],
        "warnings": [],
        "self_check": None,
    }


def _error_report(
    *,
    out_dir: Path,
    data_dir: str | Path,
    exc: KGError,
    config: dict[str, Any] | None,
    request: dict[str, Any],
) -> dict[str, Any]:
    report = _base_report(data_dir=data_dir, config=config, request=request)
    report["errors"].append(exc.to_dict())
    report["run_id"] = stable_id(
        "run",
        {
            "error": exc.code,
            "data_dir": str(Path(data_dir)),
            "request": request,
        },
    )
    atomic_write_json(out_dir / "report.json", report)
    _write_log(
        out_dir,
        [
            {
                "stage": "pipeline",
                "status": "error",
                "code": exc.code,
                "message": exc.message,
            }
        ],
    )
    return report


def run_pipeline(
    *,
    data_dir: str | Path,
    out_dir: str | Path,
    config_path: str | Path | None = None,
    overrides: dict[str, Any] | None = None,
    requested_mode: str = "auto",
    snapshot_dir: str | Path | None = None,
    graph_version: int | None = None,
    self_check: bool = True,
) -> dict[str, Any]:
    data_path = Path(data_dir)
    output_path = Path(out_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    request = {
        "requested_mode": requested_mode,
        "overrides": dict(sorted((overrides or {}).items())),
        "graph_version": graph_version,
        "self_check": self_check,
    }
    config: dict[str, Any] | None = None
    events: list[dict[str, Any]] = []
    try:
        config = with_overrides(load_config(config_path), overrides or {})
        events.append(
            {
                "stage": "config",
                "status": "ok",
                "config_version": config["config_version"],
                "config_checksum": config["checksum"],
            }
        )
        corpus = load_corpus(data_path)
        events.append(
            {
                "stage": "ingest",
                "status": "ok",
                "shard_count": corpus.shard_count,
                "document_count": len(corpus.documents),
                "sentence_count": corpus.sentence_count,
                "char_count": corpus.char_count,
            }
        )
        entity_dictionary = EntityDictionary.load(data_path / "dictionary.json")
        schema = RelationSchema.load(data_path / "relation_schema.json")
        truth = load_truth(data_path)
        events.append(
            {
                "stage": "resource_validation",
                "status": "ok",
                "dictionary_version": entity_dictionary.version,
                "dictionary_checksum": entity_dictionary.checksum,
                "relation_schema_version": schema.version,
                "relation_schema_checksum": schema.checksum,
                "truth_version": truth["truth_version"],
                "truth_checksum": truth["checksum"],
            }
        )

        entities_by_doc: dict[str, DocumentEntities] = {}
        for document in corpus.documents:
            entities_by_doc[document.doc_id] = recognize_document(
                document, entity_dictionary
            )
        events.append(
            {
                "stage": "entity_recognition",
                "status": "ok",
                "entity_count": sum(
                    len(entities.spans) for entities in entities_by_doc.values()
                ),
            }
        )

        selection = decide_path(
            corpus=corpus,
            entities_by_doc=entities_by_doc,
            schema=schema,
            config=config,
            requested_mode=requested_mode,
        )
        events.append(
            {
                "stage": "adaptive_selection",
                "status": "ok",
                "chosen_path": selection["chosen_path"],
                "decision_sha256": selection["decision_sha256"],
            }
        )
        selected_paths: list[str] = selection["chosen_paths"]
        paths: dict[str, dict[str, list[CandidateTriple]]] = {}
        for path in selected_paths:
            paths[path] = {
                document.doc_id: extract_relations(
                    document=document,
                    entities=entities_by_doc[document.doc_id],
                    schema=schema,
                    mode=path,
                    neighbor_limit=int(selection["neighbor_limit"]),
                )
                for document in corpus.documents
            }
        canonical_path = "exact" if "exact" in paths else "approx"
        mandatory_lines = {
            pattern.line_no for pattern in schema.mandatory_patterns
        }
        mandatory_check = None
        if "approx" in paths:
            if "exact" in paths:
                exact_for_mandatory = [
                    item for items in paths["exact"].values() for item in items
                ]
            else:
                exact_for_mandatory = [
                    item
                    for document in corpus.documents
                    for item in extract_relations(
                        document=document,
                        entities=entities_by_doc[document.doc_id],
                        schema=schema,
                        mode="exact",
                        neighbor_limit=int(selection["neighbor_limit"]),
                    )
                    if item.pattern_line in mandatory_lines
                ]
            mandatory_check = _mandatory_preserved(
                exact=exact_for_mandatory,
                approximate=[
                    item for items in paths["approx"].values() for item in items
                ],
                mandatory_lines=mandatory_lines,
            )
        events.append(
            {
                "stage": "relation_extraction",
                "status": "ok",
                "canonical_path": canonical_path,
                "triple_count": sum(
                    len(items) for items in paths[canonical_path].values()
                ),
            }
        )

        normalized_docs: dict[str, dict[str, Any]] = {}
        for document in corpus.documents:
            normalized_docs[document.doc_id] = normalize_document(
                document=document,
                entities=entities_by_doc[document.doc_id],
                candidates=paths[canonical_path][document.doc_id],
                entity_dictionary=entity_dictionary,
            )
        graph_stats = recompute_graph_stats(corpus, normalized_docs)
        events.append(
            {
                "stage": "normalization_and_graph",
                "status": "ok",
                "triple_count": graph_stats["triple_count"],
                "unique_canonical_entity_count": graph_stats[
                    "unique_canonical_entity_count"
                ],
            }
        )
        evaluation = evaluate_truth(truth, normalized_docs)
        cross_path = None
        report_errors: list[dict[str, Any]] = []
        if "exact" in paths and "approx" in paths:
            cross_path = _cross_path_report(
                corpus=corpus,
                exact_by_doc=paths["exact"],
                approx_by_doc=paths["approx"],
                schema=schema,
            )
            if cross_path.get("error"):
                report_errors.append(cross_path["error"])

        if mandatory_check is not None and not mandatory_check["preserved"]:
            report_errors.append(
                error_dict(
                    "E_INCONSISTENT",
                    "所选路径遗漏强制高置信模式命中",
                    category="MANDATORY_MISS",
                    missing=mandatory_check["missing"],
                )
            )

        run_id = stable_id(
            "run",
            {
                "corpus": corpus.input_checksum,
                "dictionary": entity_dictionary.checksum,
                "relation_schema": schema.checksum,
                "truth": truth["checksum"],
                "config": config["checksum"],
                "requested_mode": requested_mode,
                "chosen_path": selection["chosen_path"],
                "neighbor_limit": selection["neighbor_limit"],
            },
        )
        report = _base_report(
            data_dir=data_path,
            config=config,
            request=request,
        )
        report["run_id"] = run_id
        report["input"] = {
            "corpus_checksum": corpus.input_checksum,
            "shard_checksums": corpus.shard_checksums,
            "dictionary": entity_dictionary.checksum_record(),
            "relation_schema": schema.checksum_record(),
            "truth": {
                "version": truth["truth_version"],
                "checksum": truth["checksum"],
                "triple_count": len(truth.get("triples", [])),
            },
        }
        report["selection"] = selection
        report["degradation"] = selection.get("degradation")
        report["stages"] = {
            "ingest": {
                "shard_count": corpus.shard_count,
                "document_count": len(corpus.documents),
                "sentence_count": corpus.sentence_count,
                "char_count": corpus.char_count,
                "documents": [
                    {
                        "doc_id": document.doc_id,
                        "shard_ids": list(document.shard_ids),
                        "sentence_count": len(document.sentences),
                        "char_count": document.text_char_count,
                    }
                    for document in corpus.documents
                ],
            },
            "entity_recognition": _entity_recognition_report(
                corpus, entities_by_doc, entity_dictionary
            ),
            "relation_extraction": {
                "canonical_path": canonical_path,
                "paths": {
                    path: _relation_path_rows(items) for path, items in paths.items()
                },
                "mandatory_pattern_lines": sorted(mandatory_lines),
                "mandatory_preservation": mandatory_check,
                "tie_break_order": [
                    "confidence_desc",
                    "head_start",
                    "tail_start",
                    "relation_type_id",
                    "sentence_no",
                    "head_end",
                    "tail_end",
                    "pattern_line",
                    "trigger_offset",
                ],
            },
            "normalization": {
                "documents": normalized_docs,
                "deduplication_key": [
                    "doc_id",
                    "head_entity_id",
                    "tail_entity_id",
                    "relation_type",
                ],
            },
            "graph": {
                "statistics": graph_stats,
                "recompute_basis": "normalization.documents",
                "statistics_checksum": content_checksum(graph_stats),
            },
        }
        report["evaluation"] = evaluation
        if cross_path is not None:
            report["cross_path"] = cross_path
        report["errors"] = report_errors
        if selection.get("degradation"):
            degradation = selection["degradation"]
            report["warnings"].append(
                {
                    "code": degradation["error_code"],
                    "message": "精确路径超出预算或资源上限，已按策略降级到近似路径",
                    "category": degradation["category"],
                    "affected_docs": degradation["affected_docs"],
                    "neighbor_limit": degradation["neighbor_limit"],
                }
            )

        if self_check:
            verification = verify_execution_report(report, data_path)
            report["self_check"] = verification
            if not verification["valid"]:
                report["errors"].extend(verification["issues"])

        snapshot_store = SnapshotStore(
            snapshot_dir if snapshot_dir is not None else output_path / "snapshots"
        )
        installs: list[dict[str, Any]] = []
        if not report["errors"]:
            try:
                installs.append(
                    snapshot_store.install(
                        "config",
                        int(config["config_version"]),
                        config,
                    )
                )
                installs.append(
                    snapshot_store.install(
                        "dictionary",
                        entity_dictionary.version,
                        entity_dictionary.to_dict(),
                    )
                )
                installs.append(
                    snapshot_store.install(
                        "relation_schema",
                        schema.version,
                        schema.to_dict(),
                    )
                )
                selected_graph_version = (
                    graph_version
                    if graph_version is not None
                    else snapshot_store.next_version("graph_stats")
                )
                graph_payload = {
                    "graph_version": selected_graph_version,
                    "statistics": graph_stats,
                    "statistics_checksum": content_checksum(graph_stats),
                    "source": {
                        "run_id": run_id,
                        "corpus_checksum": corpus.input_checksum,
                        "dictionary_version": entity_dictionary.version,
                        "dictionary_checksum": entity_dictionary.checksum,
                        "relation_schema_version": schema.version,
                        "relation_schema_checksum": schema.checksum,
                        "canonical_path": canonical_path,
                    },
                }
                installs.append(
                    snapshot_store.install(
                        "graph_stats",
                        selected_graph_version,
                        graph_payload,
                    )
                )
            except KGError as snap_exc:
                report["errors"].append(snap_exc.to_dict())
        report["snapshots"] = {
            "directory": str(snapshot_store.root),
            "installs": installs,
        }

        if report["errors"]:
            report["status"] = "error"
        elif selection.get("degradation"):
            report["status"] = "degraded"
        else:
            report["status"] = "ok"
        report["summary"] = {
            "status": report["status"],
            "chosen_path": selection["chosen_path"],
            "canonical_path": canonical_path,
            "triple_count": graph_stats["triple_count"],
            "entity_span_count": graph_stats["entity_span_count"],
            "f1": evaluation["f1"],
            "self_check_valid": (
                report["self_check"]["valid"] if report["self_check"] else None
            ),
            "error_codes": [item["code"] for item in report["errors"]],
        }
        events.append(
            {
                "stage": "self_check",
                "status": "ok" if report["self_check"] and report["self_check"]["valid"] else "failed",
                "issue_count": (
                    report["self_check"]["issue_count"]
                    if report["self_check"]
                    else None
                ),
            }
        )
        atomic_write_json(output_path / "report.json", report)
        _write_log(output_path, events)
        return report
    except KGError as exc:
        return _error_report(
            out_dir=output_path,
            data_dir=data_path,
            exc=exc,
            config=config,
            request=request,
        )

