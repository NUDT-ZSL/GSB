"""Application services used by the CLI and the acceptance demo."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from .config_io import read_config_document, sha256_file
from .errors import IntentError, unattributed_record
from .injection import FaultInjector
from .io_utils import read_samples
from .matching import (
    MatchBatch,
    ResourceLimits,
    match_document,
)
from .migration import MigrationResult, migrate_document, prepare_for_matching
from .models import DEFAULT_THRESHOLD, ConfigDocument, MatchResult
from .snapshots import SnapshotStore
from .writers import write_config_document

REPORT_SCHEMA_VERSION = "1.0"


def _digest_or_error(path: str | Path) -> str:
    return sha256_file(path)


def _inputs(**paths: str) -> dict[str, Any]:
    return {
        name: {"path": str(path), "sha256": _digest_or_error(path)}
        for name, path in sorted(paths.items())
    }


def _error_report(kind: str, exc: IntentError, **extra: Any) -> dict[str, Any]:
    report = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "kind": kind,
        "status": exc.code,
        "code": exc.code,
        "error": exc.to_record(),
    }
    report.update(extra)
    return report


def _unexpected_report(kind: str, stage: str, exc: BaseException, **extra: Any) -> dict[str, Any]:
    report = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "kind": kind,
        "status": "E_UNATTRIBUTED",
        "code": "E_UNATTRIBUTED",
        "unattributed_errors": [unattributed_record(stage, exc)],
    }
    report.update(extra)
    return report


def load_prepared(
    path: str | Path,
    *,
    injector: FaultInjector | None = None,
) -> tuple[ConfigDocument, MigrationResult]:
    source = read_config_document(path, injector=injector)
    migration = prepare_for_matching(source)
    if injector is not None:
        injector.fire(
            "migrate",
            {
                "path": str(path),
                "line_no": (
                    migration.document.intents[0].line_no
                    if migration.document.intents
                    else 1
                ),
                "document": migration.document,
            },
        )
    return source, migration


def _result_comparison(
    versions: dict[str, MatchResult],
) -> tuple[list[str], dict[str, Any]]:
    labels = list(versions)
    if not labels:
        return [], {}
    reference = versions[labels[0]].to_dict()
    fields: dict[str, Any] = {
        "intent": reference["intent"],
        "matched_intent": reference["matched_intent"],
        "offsets": reference["offset_pairs"],
        "keywords": [hit["keyword"] for hit in reference["hits"]],
        "confidence": reference["confidence"],
        "fallback_category": reference["fallback_category"],
        "trigger_label": reference["trigger_label"],
        "top_hit": reference["top_hit"],
    }
    differences: list[str] = []
    for label in labels[1:]:
        candidate = versions[label].to_dict()
        candidate_fields = {
            "intent": candidate["intent"],
            "matched_intent": candidate["matched_intent"],
            "offsets": candidate["offset_pairs"],
            "keywords": [hit["keyword"] for hit in candidate["hits"]],
            "confidence": candidate["confidence"],
            "fallback_category": candidate["fallback_category"],
            "trigger_label": candidate["trigger_label"],
            "top_hit": candidate["top_hit"],
        }
        for field_name, value in candidate_fields.items():
            if value != fields[field_name] and field_name not in differences:
                differences.append(field_name)
    return differences, fields


def _comparison_between(
    left: MatchResult,
    right: MatchResult,
) -> list[str]:
    differences, _ = _result_comparison({"left": left, "right": right})
    return differences


def _capability_losses(
    document: ConfigDocument,
) -> dict[str, list[str]]:
    losses: dict[str, list[str]] = {}
    if document.source_version != "v3":
        return losses
    for spec in document.intents:
        threshold = (
            spec.threshold if spec.threshold is not None else DEFAULT_THRESHOLD
        )
        if abs(threshold - DEFAULT_THRESHOLD) > 1e-9:
            losses.setdefault("threshold", []).append(spec.intent)
        fallback = spec.fallback_intent or document.global_fallback
        if fallback != document.global_fallback:
            losses.setdefault("fallback_intent", []).append(spec.intent)
        path = spec.label_path or (spec.intent,)
        if path != (spec.intent,):
            losses.setdefault("label_path", []).append(spec.intent)
        if spec.slots:
            losses.setdefault("slots", []).append(spec.intent)
        weights = spec.weight_vector or (1.0,) * len(spec.keywords)
        if any(abs(weight - 1.0) > 1e-9 for weight in weights):
            losses.setdefault("weight_vector", []).append(spec.intent)
    return losses


def adjudicate(
    *,
    samples_path: str | Path,
    v1_path: str | Path,
    v2_path: str | Path,
    v3_path: str | Path,
    limits: ResourceLimits | None = None,
    retired_policy: str = "fallback",
) -> tuple[dict[str, Any], int]:
    """Load all three forms, migrate to v3, match and adjudicate."""

    paths = {"samples": samples_path, "v1": v1_path, "v2": v2_path, "v3": v3_path}
    source_docs: dict[str, ConfigDocument] = {}
    migrations: dict[str, MigrationResult] = {}
    batches: dict[str, MatchBatch] = {}
    try:
        inputs = _inputs(**paths)
        samples = read_samples(samples_path)
        for label, path in (("v1", v1_path), ("v2", v2_path), ("v3", v3_path)):
            document = read_config_document(path)
            if document.source_version != label:
                raise IntentError(
                    "E_CONFIG_INVALID",
                    "配置实际版本与参数位置不一致",
                    stage="read",
                    field="source_version",
                    expected=label,
                    actual=document.source_version,
                    path=str(path),
                )
            migration = prepare_for_matching(document)
            batch = match_document(
                samples,
                migration.document,
                limits=limits,
                retired_policy=retired_policy,
            )
            source_docs[label] = document
            migrations[label] = migration
            batches[label] = batch
    except IntentError as exc:
        return _error_report("cross_version_adjudication", exc, inputs=locals().get("inputs")), 2
    except Exception as exc:
        return _unexpected_report(
            "cross_version_adjudication",
            "adjudicate",
            exc,
            inputs=locals().get("inputs"),
        ), 1

    result_by_version = {
        label: {result.query_id: result for result in batch.results}
        for label, batch in batches.items()
    }
    query_rows: list[dict[str, Any]] = []
    divergent_query_ids: list[str] = []
    divergent_fields: list[str] = []
    for sample in samples:
        versions = {
            label: result_by_version[label][sample.query_id]
            for label in ("v1", "v2", "v3")
        }
        differences, canonical_fields = _result_comparison(versions)
        if differences:
            divergent_query_ids.append(sample.query_id)
            for field_name in differences:
                if field_name not in divergent_fields:
                    divergent_fields.append(field_name)
        query_rows.append(
            {
                "query_id": sample.query_id,
                "query_text": sample.query_text,
                "consistent": not differences,
                "differences": differences,
                "canonical_fields": canonical_fields,
                "versions": {
                    label: result.to_dict() for label, result in versions.items()
                },
            }
        )

    capability_losses = _capability_losses(source_docs["v3"])
    capability_query_ids: set[str] = set()
    if capability_losses:
        affected_intents = {
            intent for intents in capability_losses.values() for intent in intents
        }
        for result in batches["v3"].results:
            if (
                result.matched_intent in affected_intents
                or result.trigger_label in affected_intents
            ):
                capability_query_ids.add(result.query_id)

    semantic_loss = bool(capability_losses or divergent_query_ids)
    migration_losses = {
        label: migration.to_report()
        for label, migration in migrations.items()
        if migration.semantic_loss
    }
    status = "E_SEMANTIC_LOSS" if semantic_loss else "OK"
    report: dict[str, Any] = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "kind": "cross_version_adjudication",
        "status": status,
        "code": "E_SEMANTIC_LOSS" if semantic_loss else None,
        "inputs": inputs,
        "configs": {
            label: {
                "snapshot": source_docs[label].snapshot_summary(),
                "migration": migrations[label].to_report(),
                "self_check": batches[label].self_check,
            }
            for label in ("v1", "v2", "v3")
        },
        "queries": query_rows,
        "divergence": {
            "query_ids": divergent_query_ids,
            "fields": divergent_fields,
        },
        "semantic_loss": (
            {
                "unrestorable_fields": capability_losses,
                "query_ids": sorted(capability_query_ids),
                "migration_losses": migration_losses,
                "divergence_fields": divergent_fields,
                "divergence_query_ids": divergent_query_ids,
            }
            if semantic_loss
            else None
        ),
        "conclusion": (
            None
            if semantic_loss
            else {
                "canonical_source": "v3",
                "results": [result.to_dict() for result in batches["v3"].results],
            }
        ),
    }
    return report, 2 if semantic_loss else 0


def match_single(
    *,
    samples_path: str | Path,
    config_path: str | Path,
    limits: ResourceLimits | None = None,
    retired_policy: str = "fallback",
) -> tuple[dict[str, Any], int]:
    paths = {"samples": samples_path, "config": config_path}
    try:
        inputs = _inputs(**paths)
        samples = read_samples(samples_path)
        source = read_config_document(config_path)
        migration = prepare_for_matching(source)
        batch = match_document(
            samples,
            migration.document,
            limits=limits,
            retired_policy=retired_policy,
        )
    except IntentError as exc:
        return _error_report("match", exc, inputs=locals().get("inputs")), 2
    except Exception as exc:
        return _unexpected_report("match", "match", exc, inputs=locals().get("inputs")), 1
    report = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "kind": "match",
        "status": "OK",
        "inputs": inputs,
        "source_snapshot": source.snapshot_summary(),
        "migration": migration.to_report(),
        "match": batch.to_report(),
    }
    return report, 0


def migration_report(
    *,
    config_path: str | Path,
    target_version: str,
    out_path: str | Path,
    from_version: str | None = None,
    hierarchy_policy: str = "drop",
    loss_policy: str = "allow",
    samples_path: str | Path | None = None,
) -> tuple[dict[str, Any], int]:
    paths: dict[str, Any] = {"config": config_path}
    if samples_path is not None:
        paths["samples"] = samples_path
    try:
        inputs = _inputs(**paths)
        source = read_config_document(config_path)
        if from_version is not None and source.source_version != from_version:
            raise IntentError(
                "E_CONFIG_INVALID",
                "配置实际版本与 from_version 不一致",
                stage="migrate",
                field="from_version",
                expected=from_version,
                actual=source.source_version,
            )
        result = migrate_document(
            source,
            target_version,
            hierarchy_policy=hierarchy_policy,
            loss_policy=loss_policy,
        )
        written = write_config_document(result.document, out_path, version=target_version)
    except IntentError as exc:
        return _error_report("migration", exc, inputs=locals().get("inputs")), 2
    except Exception as exc:
        return _unexpected_report("migration", "migrate", exc, inputs=locals().get("inputs")), 1

    affected_query_ids: list[str] = []
    if samples_path is not None and result.affected_intents:
        try:
            samples = read_samples(samples_path)
            source_batch = match_document(samples, prepare_for_matching(source).document)
            affected = set(result.affected_intents)
            affected_query_ids = [
                row.query_id
                for row in source_batch.results
                if row.matched_intent in affected or row.trigger_label in affected
            ]
        except IntentError:
            affected_query_ids = []
    status = "E_SEMANTIC_LOSS" if result.semantic_loss else "OK"
    report = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "kind": "migration",
        "status": status,
        "code": "E_SEMANTIC_LOSS" if result.semantic_loss else None,
        "inputs": inputs,
        "hierarchy_policy": hierarchy_policy,
        "loss_policy": loss_policy,
        "migration": result.to_report(),
        "affected_query_ids": affected_query_ids,
        "output": {
            "path": str(out_path),
            "sha256": _digest_or_error(out_path),
            "snapshot": written.snapshot_summary(),
        },
    }
    return report, 2 if result.semantic_loss else 0


def snapshot_scenario(
    *,
    samples_path: str | Path,
    initial_path: str | Path,
    replacement_path: str | Path,
    retired_policy: str = "fallback",
    limits: ResourceLimits | None = None,
) -> tuple[dict[str, Any], int]:
    paths = {
        "samples": samples_path,
        "initial": initial_path,
        "replacement": replacement_path,
    }
    store = SnapshotStore()
    try:
        inputs = _inputs(**paths)
        samples = read_samples(samples_path)
        initial_load = store.load_file(initial_path, namespace="intent-config")
        initial_migration = prepare_for_matching(initial_load.snapshot.document)
        initial_batch = match_document(
            samples,
            initial_migration.document,
            limits=limits,
        )
        replacement_load = store.load_file(replacement_path, namespace="intent-config")
        replacement_migration = prepare_for_matching(replacement_load.snapshot.document)
        replacement_batch = match_document(
            samples,
            replacement_migration.document,
            limits=limits,
            retired_policy=retired_policy,
        )
    except IntentError as exc:
        report = _error_report(
            "snapshot_replacement",
            exc,
            inputs=locals().get("inputs"),
            substitutions=(
                {
                    "initial": locals()["initial_load"].to_report()
                    if "initial_load" in locals()
                    else None,
                    "replacement": locals()["replacement_load"].to_report()
                    if "replacement_load" in locals()
                    else None,
                }
            ),
        )
        return report, 2
    except Exception as exc:
        return _unexpected_report(
            "snapshot_replacement",
            "snapshot",
            exc,
            inputs=locals().get("inputs"),
        ), 1

    initial_by_id = {result.query_id: result for result in initial_batch.results}
    replacement_by_id = {
        result.query_id: result for result in replacement_batch.results
    }
    retired_results = [
        result
        for result in replacement_batch.results
        if result.fallback_category == "label_retired"
    ]
    affected_ids = {result.query_id for result in retired_results}
    unaffected_ids = [
        sample.query_id
        for sample in samples
        if sample.query_id not in affected_ids
    ]
    changed_unaffected: list[dict[str, Any]] = []
    for query_id in unaffected_ids:
        differences = _comparison_between(
            initial_by_id[query_id],
            replacement_by_id[query_id],
        )
        if differences:
            changed_unaffected.append(
                {"query_id": query_id, "fields": differences}
            )

    rollback_guard: dict[str, Any]
    version_increased = (
        replacement_load.snapshot.version > initial_load.snapshot.version
    )
    if not version_increased:
        rollback_guard = {
            "status": "NOT_APPLICABLE",
            "guard_triggered": False,
            "reason": "replacement_is_not_a_newer_version",
        }
    else:
        try:
            store.load_file(initial_path, namespace="intent-config")
        except IntentError as exc:
            rollback_guard = {
                "status": exc.code,
                "guard_triggered": exc.code == "E_SNAPSHOT_ROLLBACK",
                "error": exc.to_record(),
            }
        else:
            rollback_guard = {
                "status": "E_INCONSISTENT",
                "guard_triggered": False,
                "error": {
                    "code": "E_INCONSISTENT",
                    "message": "旧快照覆盖了新快照",
                    "category": "snapshot_rollback_guard",
                },
            }

    if changed_unaffected:
        report = {
            "schema_version": REPORT_SCHEMA_VERSION,
            "kind": "snapshot_replacement",
            "status": "E_INCONSISTENT",
            "code": "E_INCONSISTENT",
            "inputs": inputs,
            "error": {
                "code": "E_INCONSISTENT",
                "message": "标签收缩改变了未受影响查询",
                "category": "snapshot_unaffected_changed",
                "changed": changed_unaffected,
            },
        }
        return report, 2

    rollback_ok = not version_increased or rollback_guard["guard_triggered"]
    report = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "kind": "snapshot_replacement",
        "status": "OK" if rollback_ok else "E_INCONSISTENT",
        "code": None if rollback_ok else "E_INCONSISTENT",
        "inputs": inputs,
        "retired_policy": retired_policy,
        "initial": {
            "load": initial_load.to_report(),
            "migration": initial_migration.to_report(),
            "self_check": initial_batch.self_check,
        },
        "replacement": {
            "load": replacement_load.to_report(),
            "migration": replacement_migration.to_report(),
            "self_check": replacement_batch.self_check,
        },
        "rollback_guard": rollback_guard,
        "retired": {
            "count": len(retired_results),
            "query_ids": [result.query_id for result in retired_results],
            "records": [result.to_dict() for result in retired_results],
        },
        "unaffected": {
            "query_ids": unaffected_ids,
            "changed": changed_unaffected,
        },
    }
    return report, 0


def injection_run(
    *,
    samples_path: str | Path,
    config_path: str | Path,
    injector: FaultInjector,
    limits: ResourceLimits | None = None,
    retired_policy: str = "fallback",
) -> tuple[dict[str, Any], int]:
    paths = {"samples": samples_path, "config": config_path}
    report: dict[str, Any] = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "kind": "fault_injection",
        "status": "OK",
        "injections": [spec.to_dict() for spec in injector.specs],
        "inputs": {},
        "pipeline_stages": [],
        "attributed_errors": [],
        "unattributed_errors": [],
        "triggered": [],
        "unreached": [],
    }
    current_stage = "input"
    try:
        report["inputs"] = _inputs(**paths)
        samples = read_samples(samples_path)
        current_stage = "read"
        report["pipeline_stages"].append({"stage": "read", "status": "started"})
        source = read_config_document(config_path, injector=injector)
        report["pipeline_stages"][-1]["status"] = "OK"
        current_stage = "migrate"
        report["pipeline_stages"].append({"stage": "migrate", "status": "started"})
        migration = prepare_for_matching(source)
        injector.fire(
            "migrate",
            {
                "path": str(config_path),
                "line_no": (
                    migration.document.intents[0].line_no
                    if migration.document.intents
                    else 1
                ),
            },
        )
        report["pipeline_stages"][-1]["status"] = "OK"
        current_stage = "match"
        report["pipeline_stages"].append({"stage": "match", "status": "started"})
        batch = match_document(
            samples,
            migration.document,
            limits=limits,
            retired_policy=retired_policy,
            injector=injector,
        )
        report["pipeline_stages"][-1]["status"] = "OK"
        report["results"] = [result.to_dict() for result in batch.results]
        report["self_check"] = batch.self_check
    except IntentError as exc:
        record = exc.to_record(attributed=True)
        report["attributed_errors"].append(record)
        report["status"] = exc.code
        report["code"] = exc.code
        if report["pipeline_stages"] and report["pipeline_stages"][-1]["status"] == "started":
            report["pipeline_stages"][-1]["status"] = exc.code
    except Exception as exc:
        record = unattributed_record(current_stage, exc)
        if injector.triggered:
            context = injector.triggered[-1].get("context", {})
            for key in ("query_id", "line_no"):
                if key in context:
                    record[key] = context[key]
        report["unattributed_errors"].append(record)
        report["status"] = "E_UNATTRIBUTED"
        report["code"] = "E_UNATTRIBUTED"
        if report["pipeline_stages"] and report["pipeline_stages"][-1]["status"] == "started":
            report["pipeline_stages"][-1]["status"] = "E_UNATTRIBUTED"
    report["triggered"] = list(injector.triggered)
    report["unreached"] = injector.unreached()
    success = report["status"] == "OK"
    return report, 0 if success else (1 if report["status"] == "E_UNATTRIBUTED" else 2)


def compare_result_sets(
    left: Iterable[MatchResult],
    right: Iterable[MatchResult],
) -> list[dict[str, Any]]:
    left_map = {row.query_id: row for row in left}
    right_map = {row.query_id: row for row in right}
    differences: list[dict[str, Any]] = []
    for query_id in sorted(set(left_map) | set(right_map)):
        if query_id not in left_map or query_id not in right_map:
            differences.append(
                {
                    "query_id": query_id,
                    "fields": ["missing_result"],
                }
            )
            continue
        fields = _comparison_between(left_map[query_id], right_map[query_id])
        if fields:
            differences.append({"query_id": query_id, "fields": fields})
    return differences
