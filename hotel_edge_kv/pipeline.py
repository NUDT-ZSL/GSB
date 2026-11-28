from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from .audit import audit_scheduler
from .models import Scenario
from .reporting import (
    block_ledger_rows,
    build_summary_markdown,
    lifecycle_rows,
    write_csv,
    write_json,
    write_text,
)
from .scheduler import Scheduler, TERMINAL_STATUSES


def _terminal_view(scheduler: Scheduler) -> list[dict[str, Any]]:
    view = []
    for request in scheduler.requests:
        runtime = scheduler.runtimes[request.request_id]
        view.append(
            {
                "request_id": request.request_id,
                "ticket_id": request.ticket_id,
                "status": runtime.status,
                "tokens_generated": runtime.tokens_generated,
                "terminal_at": runtime.terminal_at,
                "cancel_reason": runtime.cancel_reason,
                "cancel_stage": runtime.cancel_stage,
                "cancel_effective_at": runtime.cancel_effective_at,
                "reclaimed_at": runtime.reclaimed_at,
            }
        )
    return view


def _full_ledger_view(scheduler: Scheduler) -> dict[str, Any]:
    return {
        "lifecycle": [entry.to_dict() for entry in scheduler.lifecycle],
        "block_ledger": [entry.to_dict() for entry in scheduler.block_ledger],
        "token_events": list(scheduler.token_events),
        "cancellation_records": list(scheduler.cancellation_records),
        "competition_records": list(scheduler.competition_records),
        "reuse_links": list(scheduler.reuse_links),
        "memory_samples": list(scheduler.memory_samples),
    }


def _build_resume_report(
    checkpoint: dict[str, Any],
    prefix: Scheduler,
    before_restore: dict[str, Any],
    restored: Scheduler,
    after_restore: dict[str, Any],
    resumed: Scheduler,
    reference: Scheduler,
) -> dict[str, Any]:
    checkpoint_time = int(prefix.current_time)
    resumed_checkpoint_time = int(restored.current_time)
    cancelled_ids = sorted(
        request_id
        for request_id, runtime in prefix.runtimes.items()
        if runtime.status == "cancelled"
    )
    post_restore_cancelled = [
        request_id
        for request_id, runtime in restored.runtimes.items()
        if runtime.status == "cancelled"
    ]
    cancelled_all_terminal = set(cancelled_ids).issubset(set(post_restore_cancelled))
    cancelled_in_queue = sorted(
        set(cancelled_ids) & set(restored.queue)
    )
    cancelled_in_active = sorted(
        {
            item["request_id"]
            for item in after_restore["active_requests"]
        }
        & set(cancelled_ids)
    )
    later_readmissions = [
        {
            "request_id": entry.request_id,
            "ticket_id": entry.ticket_id,
            "time": entry.time,
        }
        for entry in resumed.lifecycle
        if entry.time > checkpoint_time
        and entry.event == "slot_acquired"
        and entry.request_id in set(cancelled_ids)
    ]

    terminal_equal = _terminal_view(resumed) == _terminal_view(reference)
    resumed_ledger = _full_ledger_view(resumed)
    reference_ledger = _full_ledger_view(reference)
    ledger_components = {
        name: resumed_ledger[name] == reference_ledger[name]
        for name in resumed_ledger
    }
    final_snapshot_equal = resumed.occupancy_snapshot() == reference.occupancy_snapshot()

    before_fields = {
        key: before_restore[key]
        for key in (
            "time",
            "used_blocks",
            "free_blocks",
            "used_bytes",
            "block_owners",
            "free_block_ids",
            "queued_request_ids",
            "active_requests",
            "conservation_ok",
            "budget_ok",
        )
    }
    after_fields = {
        key: after_restore[key]
        for key in (
            "time",
            "used_blocks",
            "free_blocks",
            "used_bytes",
            "block_owners",
            "free_block_ids",
            "queued_request_ids",
            "active_requests",
            "conservation_ok",
            "budget_ok",
        )
    }
    snapshot_equal = before_fields == after_fields
    remaining_at_checkpoint = sum(
        1
        for runtime in restored.runtimes.values()
        if runtime.status not in TERMINAL_STATUSES
    )
    return {
        "checkpoint_schema_version": checkpoint["schema_version"],
        "checkpoint_time": checkpoint_time,
        "restored_time": resumed_checkpoint_time,
        "snapshot_equal": snapshot_equal,
        "pre_restart_snapshot": before_fields,
        "post_restore_snapshot": after_fields,
        "snapshot_comparison": {
            key: before_fields[key] == after_fields[key] for key in before_fields
        },
        "cancelled_request_ids": cancelled_ids,
        "cancelled_requests_remain_terminal": cancelled_all_terminal,
        "cancelled_requests_in_queue_after_restore": cancelled_in_queue,
        "cancelled_requests_active_after_restore": cancelled_in_active,
        "cancelled_requests_readmitted_after_checkpoint": later_readmissions,
        "released_blocks_free_after_restore": after_fields["free_block_ids"],
        "remaining_non_terminal_requests_at_checkpoint": remaining_at_checkpoint,
        "continued_to_time": resumed.current_time,
        "terminal_state_matches_reference": terminal_equal,
        "final_snapshot_matches_reference": final_snapshot_equal,
        "ledger_component_checks": ledger_components,
        "matches_reference": (
            terminal_equal
            and final_snapshot_equal
            and all(ledger_components.values())
        ),
        "reference_final_snapshot": reference.occupancy_snapshot(),
        "resumed_final_snapshot": resumed.occupancy_snapshot(),
        "notes": [
            "续算从持久化 checkpoint 构造新 Scheduler，不重放已处理取消事件。",
            "恢复前后比较块所有者、空闲块、排队请求、活动请求与显存占用。",
            "续算终态与同一输入的不间断基准逐账本比较。",
        ],
    }


def run_pipeline(
    scenario: Scenario,
    output_dir: str | Path = "results",
    resume_at: int | None = None,
) -> dict[str, Any]:
    scenario.validate()
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    checkpoint_time = (
        int(resume_at) if resume_at is not None else int(scenario.resume_at)
    )
    if checkpoint_time < 0:
        checkpoint_time = 0

    write_json(root / "inputs" / "scenario.json", scenario.to_dict())
    write_json(root / "inputs" / "config.json", scenario.config.to_dict())
    write_json(
        root / "inputs" / "requests.json",
        [request.to_dict() for request in scenario.requests],
    )
    write_json(
        root / "inputs" / "cancellations.json",
        [event.to_dict() for event in scenario.cancellations],
    )

    reference = Scheduler(
        scenario.config, scenario.requests, scenario.cancellations
    )
    reference.run()

    prefix = Scheduler(scenario.config, scenario.requests, scenario.cancellations)
    prefix.run_until(checkpoint_time)
    checkpoint = prefix.to_checkpoint()
    write_json(root / "checkpoint" / "resume_checkpoint.json", checkpoint)

    before_restore = prefix.occupancy_snapshot()
    restored = Scheduler.from_checkpoint(checkpoint)
    after_restore = restored.occupancy_snapshot()
    resumed = Scheduler.from_checkpoint(checkpoint)
    resumed.run()

    audit = audit_scheduler(resumed)
    cancellation_report = resumed.cancellation_summary()
    trace_by_request = resumed.request_traces()
    trace_map = {item["request_id"]: item for item in trace_by_request}
    for record in cancellation_report["cancellations"]:
        record["trace"] = trace_map[record["request_id"]]["trace"]

    competition_by_stage = Counter(
        record["stage"] for record in resumed.competition_records
    )
    competition_report = {
        "competition_count": len(resumed.competition_records),
        "by_stage": dict(sorted(competition_by_stage.items())),
        "cancel_vs_completion_count": sum(
            1
            for record in resumed.competition_records
            if "cancel_vs_completion" in record["types"]
        ),
        "multiple_cancellation_count": sum(
            1
            for record in resumed.competition_records
            if "multiple_cancellations" in record["types"]
        ),
        "decision": "cancel_wins_before_decode_at_same_logical_time",
        "multiple_cancel_tie_break": "smallest (sequence, event_id) wins",
        "records": resumed.competition_records,
        "ignored_cancellations": resumed.ignored_cancellations,
    }

    memory_timeline = resumed.dense_memory_timeline()
    resume_report = _build_resume_report(
        checkpoint=checkpoint,
        prefix=prefix,
        before_restore=before_restore,
        restored=restored,
        after_restore=after_restore,
        resumed=resumed,
        reference=reference,
    )

    write_json(
        root / "ledger" / "lifecycle.json",
        [entry.to_dict() for entry in resumed.lifecycle],
    )
    write_csv(
        root / "ledger" / "lifecycle.csv",
        lifecycle_rows(resumed.lifecycle),
        ["sequence", "time", "request_id", "ticket_id", "event", "details"],
    )
    write_json(
        root / "ledger" / "block_ledger.json",
        [entry.to_dict() for entry in resumed.block_ledger],
    )
    write_csv(
        root / "ledger" / "block_ledger.csv",
        block_ledger_rows(resumed.block_ledger),
        [
            "sequence",
            "time",
            "block_id",
            "event",
            "request_id",
            "ticket_id",
            "reason",
            "details",
        ],
    )
    write_json(root / "ledger" / "token_events.json", resumed.token_events)
    write_json(root / "ledger" / "lifecycle_by_request.json", trace_by_request)
    write_json(root / "reports" / "cancellation_report.json", cancellation_report)
    write_json(root / "reports" / "competition_report.json", competition_report)
    write_json(root / "reports" / "memory_timeline.json", memory_timeline)
    write_csv(
        root / "reports" / "memory_timeline.csv",
        memory_timeline,
        [
            "time",
            "used_blocks",
            "free_blocks",
            "used_bytes",
            "vram_budget_bytes",
            "allocatable_free_blocks",
            "conservation_ok",
            "budget_ok",
        ],
    )
    write_json(root / "reports" / "audit_report.json", audit)
    write_json(root / "reports" / "resume_report.json", resume_report)

    scheduler_summary = resumed.result_summary()
    acceptance = {
        "status": (
            "ok"
            if audit["status"] == "ok"
            and scheduler_summary["all_terminal"]
            and resume_report["matches_reference"]
            and resume_report["snapshot_equal"]
            else "failed"
        ),
        "checks": audit["checks"],
        "issue_count": audit["issue_count"],
        "resume_snapshot_equal": resume_report["snapshot_equal"],
        "resume_matches_reference": resume_report["matches_reference"],
    }
    summary = {
        "schema_version": 1,
        "scenario": scenario.scenario_name,
        "seed": scenario.seed,
        "checkpoint_time": checkpoint_time,
        "scheduler": scheduler_summary,
        "cancellation": cancellation_report["overall"],
        "competition": {
            "count": competition_report["competition_count"],
            "cancel_vs_completion": competition_report[
                "cancel_vs_completion_count"
            ],
            "multiple_cancellations": competition_report[
                "multiple_cancellation_count"
            ],
        },
        "audit": {
            "status": audit["status"],
            "issue_count": audit["issue_count"],
            "summary": audit["summary"],
        },
        "resume": {
            "checkpoint_time": resume_report["checkpoint_time"],
            "snapshot_equal": resume_report["snapshot_equal"],
            "matches_reference": resume_report["matches_reference"],
            "cancelled_requests_remain_terminal": resume_report[
                "cancelled_requests_remain_terminal"
            ],
            "cancelled_requests_readmitted_after_checkpoint": resume_report[
                "cancelled_requests_readmitted_after_checkpoint"
            ],
        },
        "acceptance": acceptance,
    }
    write_json(root / "summary.json", summary)
    write_text(root / "summary.md", build_summary_markdown(summary))
    return summary
