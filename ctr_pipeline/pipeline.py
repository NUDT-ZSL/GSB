from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .budget import plan_budget, validate_budget_config
from .canonical import atomic_write_bytes, canonical_json_bytes, checksum
from .ingestion import (
    expand_dataset,
    load_feature_dictionary,
    load_source_samples,
    write_expanded_dataset,
)
from .models import BudgetConfig, BudgetDecision, TrainingConfig
from .scheduling import validate_training_config
from .snapshots import (
    BudgetLedger,
    SnapshotStore,
    SubmissionGate,
    submit_schedule_plan,
)
from .training import (
    compare_chunked_and_full,
    train_chunked,
    train_full_batch,
    training_fingerprint,
)
from .validation import validate_result


def _plan_snapshot_payload(
    *,
    decision: BudgetDecision,
    training_config: TrainingConfig,
    budget_config: BudgetConfig,
    feature_dimension: int,
    dictionary_version: str,
    dictionary_checksum: str,
    expanded_checksum: str,
) -> dict[str, Any]:
    return {
        "budget_config": budget_config.to_dict(),
        "budget_decision": {
            "admitted_chunk_count": decision.admitted_chunk_count,
            "admitted_sample_count": len(decision.admitted_sample_ids),
            "degrade_categories": list(decision.degrade_categories),
            "degraded": decision.degraded,
            "effective_chunk_size": decision.effective_chunk_size,
            "effective_workers": decision.effective_workers,
            "estimated_macs": decision.estimated_macs,
            "estimated_memory_bytes": decision.estimated_memory_bytes,
            "rejected_sample_count": len(decision.rejected_sample_ids),
        },
        "dictionary_checksum": dictionary_checksum,
        "dictionary_version": dictionary_version,
        "expanded_checksum": expanded_checksum,
        "feature_dimension": feature_dimension,
        "schedule_report": decision.report,
        "training_config": training_config.to_dict(),
    }


def _empty_result(
    *,
    decision: BudgetDecision,
    config: TrainingConfig,
    budget_config: BudgetConfig,
    feature_dimension: int,
    dictionary_version: str,
    dictionary_checksum: str,
    expanded_checksum: str,
    source_ids: list[str],
) -> dict[str, Any]:
    error_code = (
        decision.error["code"] if decision.error is not None else None
    )
    return {
        "budget": {
            "decision": {
                "admitted_chunk_count": decision.admitted_chunk_count,
                "admitted_sample_count": len(decision.admitted_sample_ids),
                "degrade_categories": list(decision.degrade_categories),
                "degraded": decision.degraded,
                "effective_chunk_size": decision.effective_chunk_size,
                "effective_workers": decision.effective_workers,
                "estimated_macs": decision.estimated_macs,
                "estimated_memory_bytes": decision.estimated_memory_bytes,
                "rejected_sample_count": len(decision.rejected_sample_ids),
            },
            "error": decision.error,
            "report": decision.report,
        },
        "chunked": None,
        "comparison": None,
        "config": config.to_dict(),
        "dictionary": {
            "checksum": dictionary_checksum,
            "version": dictionary_version,
        },
        "determinism_fingerprint": None,
        "feature_dimension": feature_dimension,
        "full": None,
        "schema_version": 1,
        "stage_ids": {
            "admitted": [],
            "expanded": list(source_ids),
            "ingested": list(source_ids),
            "rejected": list(decision.rejected_sample_ids),
            "scored": [],
        },
        "status": "budget_rejected",
        "summary": {
            "admitted_sample_count": 0,
            "budget_error_code": error_code,
            "degrade_categories": list(decision.degrade_categories),
            "degraded": decision.degraded,
            "dictionary_checksum": dictionary_checksum,
            "dictionary_version": dictionary_version,
            "expanded_checksum": expanded_checksum,
            "metrics": None,
            "rejected_sample_count": len(decision.rejected_sample_ids),
            "sample_count": 0,
            "trained_sample_ids": [],
        },
    }


def run_pipeline(
    exposure_path: str | Path,
    feature_dictionary_path: str | Path,
    *,
    feature_dimension: int = 256,
    dictionary_version: str = "dict-v1",
    max_cross_slots: int = 8,
    training_config: TrainingConfig | None = None,
    budget_config: BudgetConfig | None = None,
    output_dir: str | Path | None = None,
    snapshot_dir: str | Path | None = None,
    snapshot_version: int = 1,
    submission_lock_path: str | Path | None = None,
    validate: bool = True,
) -> dict[str, Any]:
    config = training_config or TrainingConfig()
    budget = budget_config or BudgetConfig()
    validate_training_config(config, feature_dimension=feature_dimension)
    validate_budget_config(budget)

    dictionary = load_feature_dictionary(
        feature_dictionary_path,
        version=dictionary_version,
    )
    source_samples = load_source_samples(
        exposure_path,
        dictionary,
        max_cross_slots=max_cross_slots,
    )
    expanded = expand_dataset(
        source_samples,
        dictionary,
        feature_dimension=feature_dimension,
    )
    decision = plan_budget(
        expanded.samples,
        feature_dimension=feature_dimension,
        training_config=config,
        budget_config=budget,
    )

    if output_dir is not None:
        output = Path(output_dir)
        output.mkdir(parents=True, exist_ok=True)
        write_expanded_dataset(output / "expanded_features.jsonl", expanded)

    plan_payload = _plan_snapshot_payload(
        decision=decision,
        training_config=config,
        budget_config=budget,
        feature_dimension=feature_dimension,
        dictionary_version=dictionary.version,
        dictionary_checksum=dictionary.checksum,
        expanded_checksum=expanded.checksum,
    )
    store = SnapshotStore(snapshot_dir) if snapshot_dir is not None else None
    if store is not None:
        if submission_lock_path is not None:
            submit_schedule_plan(
                SubmissionGate(submission_lock_path),
                store,
                plan_payload,
                version=snapshot_version,
            )
        else:
            store.publish("schedule_plan", plan_payload, version=snapshot_version)

    admitted_ids = set(decision.admitted_sample_ids)
    admitted_samples = tuple(
        sample for sample in expanded.samples if sample.sample_id in admitted_ids
    )
    source_ids = list(expanded.sample_ids)
    if not admitted_samples:
        result = _empty_result(
            decision=decision,
            config=config,
            budget_config=budget,
            feature_dimension=feature_dimension,
            dictionary_version=dictionary.version,
            dictionary_checksum=dictionary.checksum,
            expanded_checksum=expanded.checksum,
            source_ids=source_ids,
        )
    else:
        chunked = train_chunked(
            admitted_samples,
            feature_dimension=feature_dimension,
            config=config,
            chunk_size=decision.effective_chunk_size,
            workers=decision.effective_workers,
        )
        full = train_full_batch(
            admitted_samples,
            feature_dimension=feature_dimension,
            config=config,
        )
        if chunked["complete"]:
            comparison = compare_chunked_and_full(chunked, full)
            status = "ok"
            scored_ids = [
                detail["sample_id"] for detail in chunked["scores"]
            ]
            summary_metrics = chunked["metrics"]
            trained_ids = scored_ids
            fingerprint = training_fingerprint(chunked, full, comparison)
        else:
            comparison = None
            status = "partial"
            scored_ids = []
            summary_metrics = None
            trained_ids = []
            fingerprint = checksum(
                {
                    "chunked": chunked,
                    "full": full,
                    "status": status,
                }
            )

        result = {
            "budget": {
                "decision": {
                    "admitted_chunk_count": decision.admitted_chunk_count,
                    "admitted_sample_count": len(decision.admitted_sample_ids),
                    "degrade_categories": list(decision.degrade_categories),
                    "degraded": decision.degraded,
                    "effective_chunk_size": decision.effective_chunk_size,
                    "effective_workers": decision.effective_workers,
                    "estimated_macs": decision.estimated_macs,
                    "estimated_memory_bytes": decision.estimated_memory_bytes,
                    "rejected_sample_count": len(decision.rejected_sample_ids),
                },
                "error": decision.error,
                "report": decision.report,
            },
            "chunked": chunked,
            "comparison": comparison,
            "config": config.to_dict(),
            "determinism_fingerprint": fingerprint,
            "dictionary": {
                "checksum": dictionary.checksum,
                "version": dictionary.version,
            },
            "feature_dimension": feature_dimension,
            "full": full,
            "schema_version": 1,
            "stage_ids": {
                "admitted": list(decision.admitted_sample_ids),
                "expanded": source_ids,
                "ingested": source_ids,
                "rejected": list(decision.rejected_sample_ids),
                "scored": scored_ids,
            },
            "status": status,
            "summary": {
                "admitted_sample_count": len(admitted_samples),
                "budget_error_code": (
                    decision.error["code"] if decision.error is not None else None
                ),
                "chunked_parameters_checksum": checksum(chunked["parameters"]),
                "degrade_categories": list(decision.degrade_categories),
                "degraded": decision.degraded,
                "determinism_fingerprint": fingerprint,
                "dictionary_checksum": dictionary.checksum,
                "dictionary_version": dictionary.version,
                "expanded_checksum": expanded.checksum,
                "full_parameters_checksum": checksum(full["parameters"]),
                "metrics": summary_metrics,
                "rejected_sample_count": len(decision.rejected_sample_ids),
                "sample_count": len(trained_ids),
                "trained_sample_ids": trained_ids,
            },
        }

    validation_report = validate_result(result, raise_on_error=validate)
    result["validation"] = validation_report

    if store is not None:
        ledger = BudgetLedger(budget.total_mac_budget)
        if decision.estimated_macs > 0:
            ledger.debit("run:estimated_macs", decision.estimated_macs)
        store.publish(
            "budget_ledger",
            {
                **ledger.snapshot_payload(),
                "estimated_memory_bytes": decision.estimated_memory_bytes,
            },
            version=snapshot_version,
        )
        store.publish(
            "training_summary",
            result["summary"],
            version=snapshot_version,
        )

    if output_dir is not None:
        atomic_write_bytes(
            Path(output_dir) / "run.json",
            canonical_json_bytes(result) + b"\n",
        )
    return result


def load_result(path: str | Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)
