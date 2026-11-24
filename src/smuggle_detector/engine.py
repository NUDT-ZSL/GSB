"""Detection engine orchestration shared by CLI and tests."""

from __future__ import annotations

from collections import defaultdict

from .checkpoint import CheckpointManager
from .detector import detect_connection
from .models import Plan, SampleInput
from .rules import build_rule_catalog
from .scheduler import SchedulerConfig, run_batch


def run_detection_batch(
    sample: SampleInput,
    plan: Plan,
    manager: CheckpointManager,
    *,
    resume: bool,
) -> dict:
    catalog = build_rule_catalog(sample.rules)
    loaded = manager.load(require_identity=True)
    residues = list(loaded.residues)
    if resume and loaded.valid:
        existing_results = dict(loaded.completed)
        resumed = True
        rebuild_reason = "none"
    else:
        existing_results = {}
        resumed = False
        rebuild_reason = loaded.reason if resume else "resume_not_requested"
        manager.reset()

    grouped_records: dict[str, list] = defaultdict(list)
    for record in sample.messages:
        grouped_records[record.connection_id].append(record)
    connection_ids = sorted(grouped_records)

    def worker(connection_id: str, timeout_ms: int) -> dict:
        return detect_connection(
            connection_id,
            grouped_records[connection_id],
            catalog,
            timeout_ms=timeout_ms,
        )

    config = SchedulerConfig(
        concurrency=plan.concurrency,
        connection_timeout_ms=plan.connection_timeout_ms,
        batch_budget_ms=plan.batch_budget_ms,
        queue_limit=plan.queue_limit,
    )
    results, summary, scheduling = run_batch(
        connection_ids,
        existing_results=existing_results,
        config=config,
        worker=worker,
        on_result=manager.commit,
    )
    verify = manager.verify()
    scheduling["resumed_results"] = len(existing_results) if resumed else 0
    checkpoint_info = {
        "resumed": resumed,
        "rebuild_reason": rebuild_reason,
        "residues_detected": residues,
        "sequence": manager.sequence,
        "completed_count": len(manager.completed),
        "verification": verify,
    }
    return {
        "catalog": catalog,
        "connections": results,
        "summary": summary,
        "scheduling": scheduling,
        "checkpoint": checkpoint_info,
    }

