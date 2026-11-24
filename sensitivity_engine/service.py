"""High-level deterministic import orchestration."""

from __future__ import annotations

from typing import Any

from .codes import Code
from .budget import WallBudget, run_pool
from .models import Batch, Dataset
from .store import Store


def prepare_batch(batch: Batch) -> Batch:
    # Parsing already normalized; this boundary makes the worker pool real and
    # keeps ordering validation independent from thread scheduling.
    sorted_ops = tuple(sorted(batch.operations, key=lambda op: op.order_key))
    return Batch(
        batch_id=batch.batch_id,
        logical_clock=batch.logical_clock,
        operations=sorted_ops,
        source_name=batch.source_name,
    )


def import_dataset(
    store: Store,
    dataset: Dataset,
    *,
    concurrency: int = 1,
    queue_limit: int = 16,
    batch_timeout_ms: float | None = None,
) -> dict[str, Any]:
    # `import` is a full deterministic synchronization from the supplied data
    # directory. Checkpoint recovery uses recover_to_checkpoint() instead.
    store.reset_state(dataset.layout_version, dataset.level_enum_version)
    base_result = store.import_base(dataset)
    recorded_rejection_count = store.record_issues(
        list(dataset.issues) + list(dataset.input_failures)
    )

    indexed, queue_rejections = run_pool(
        dataset.batches,
        prepare_batch,
        concurrency=max(1, concurrency),
        queue_limit=max(1, queue_limit),
    )
    prepared: list[Batch] = []
    for _, batch in indexed:
        if batch is not None:
            prepared.append(batch)
    prepared.sort(key=lambda batch: batch.order_key)

    batch_reports: list[dict[str, Any]] = []
    for batch in prepared:
        if not batch.operations:
            batch_issues = [
                item.to_dict()
                for item in dataset.issues
                if getattr(item, "batch_id", None) == batch.batch_id
            ]
            batch_reports.append(
                {
                    "batch_id": batch.batch_id,
                    "logical_clock": batch.logical_clock,
                    "code": Code.INVALID_RECORD,
                    "applied": False,
                    "rejections": batch_issues,
                    "events": [],
                    "reentry_set": [],
                    "changed_set": [],
                }
            )
            continue
        report = store.apply_batch(
            batch,
            budget=WallBudget.start(batch_timeout_ms) if batch_timeout_ms is not None else None,
        )
        batch_reports.append(report)
        if report.get("rejections"):
            store.record_issues(report["rejections"])
        if report.get("code") == Code.TIMEOUT:
            break

    applied_ids = {
        report["batch_id"]
        for report in batch_reports
        if report.get("code") == Code.OK and report.get("applied", True)
    }
    skipped_after_timeout = [
        batch.batch_id for batch in prepared if batch.batch_id not in applied_ids
    ]

    rejections = store.list_rejections()
    timeout_count = sum(1 for report in batch_reports if report.get("code") == "E_TIMEOUT")
    report = {
        "report_type": "sensitivity-import-report",
        "report_version": 1,
        "layout_change": store.layout_change,
        "base": base_result,
        "concurrency": concurrency,
        "queue_limit": queue_limit,
        "batch_timeout_ms": batch_timeout_ms,
        "batch_order": [batch.batch_id for batch in prepared],
        "batches": batch_reports,
        "skipped_after_timeout": skipped_after_timeout,
        "queue_rejections": queue_rejections,
        "rejections": rejections,
        "rejection_count": len(rejections),
        "recorded_rejection_count": recorded_rejection_count,
        "timeout_count": timeout_count,
        "watermark": store.watermark(),
        "ledger_digest": store.ledger_digest(),
    }
    return report
