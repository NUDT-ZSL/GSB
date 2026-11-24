from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from .canonical import atomic_write_json
from .deps import DependencyGraph, edit_changed_keys
from .editing import apply_edit_step, materialize_documents
from .ledger import Ledger
from .model import load_model
from .pipeline import (
    aggregate_completed_battles,
    build_report,
    verify_ledger_consistency,
)
from .report import atomic_write_report
from .scheduler import BudgetScheduler, LogicalClock, ManualLogicalClock


def run_evolution(
    input_dir: str | Path,
    work_dir: str | Path,
    state_dir: str | Path,
    reports_dir: str | Path,
    *,
    stop_event: threading.Event | None = None,
    clock: LogicalClock | None = None,
) -> dict[str, Any]:
    input_path = Path(input_dir)
    work_path = Path(work_dir)
    state_path = Path(state_dir)
    reports_path = Path(reports_dir)
    work_path.mkdir(parents=True, exist_ok=True)
    state_path.mkdir(parents=True, exist_ok=True)
    reports_path.mkdir(parents=True, exist_ok=True)
    main_report_path = reports_path / "balance_report.json"

    from .pipeline import run_pipeline

    initial = run_pipeline(
        input_path,
        state_path,
        main_report_path,
        stop_event=stop_event,
        clock=clock or ManualLogicalClock(),
        revision=0,
        verify=True,
    )
    if initial["status"] != "completed":
        return initial

    previous_version = initial["report_write"]["version_id"]
    current_documents = initial["model"].documents
    edit_doc = current_documents.get("edit_script.json", {})
    steps = sorted(edit_doc.get("steps", []), key=lambda item: int(item["step"]))
    step_records: list[dict[str, Any]] = []
    all_consistent = bool(initial["verification"] and initial["verification"]["consistent"])

    for step in steps:
        step_number = int(step["step"])
        step_id = f"step-{step_number:02d}"
        current_documents = apply_edit_step(current_documents, step)
        active_dir = work_path / step_id
        materialize_documents(active_dir, current_documents)
        model = load_model(active_dir)

        with Ledger(state_path / "balance.db") as ledger:
            manifests = ledger.load_dependency_manifests()
            chunk_counts = {
                battle.battle_id: battle.chunk_count
                for battle in model.battle_list()
            }
            graph = DependencyGraph.from_manifests(manifests, chunk_counts)
            changed_keys = edit_changed_keys(step)
            plan = graph.closure(changed_keys)
            plan["step_id"] = step_id
            plan["step"] = step
            ledger.apply_invalidation_plan(step_id, plan)

            scheduler = BudgetScheduler(
                model,
                ledger,
                clock=clock or ManualLogicalClock(),
                stop_event=stop_event,
            )
            schedule_result = scheduler.run_until_complete()
            if schedule_result["status"] != "completed":
                return {
                    "status": "interrupted",
                    "step_id": step_id,
                    "schedule": schedule_result,
                }

            aggregates = aggregate_completed_battles(model, ledger)
            verification = verify_ledger_consistency(model, ledger)
            all_consistent = all_consistent and verification["consistent"]
            report = build_report(
                model,
                schedule_result,
                aggregates,
                revision=step_number,
                verification=verification,
                invalidation={
                    "step_id": step_id,
                    "step": step,
                    **{
                        key: plan[key]
                        for key in (
                            "changed_keys",
                            "key_hits",
                            "direct_chunks",
                            "invalid_chunks",
                            "reasons",
                            "affected_battles",
                            "invalid_chunk_count",
                            "direct_chunk_count",
                        )
                    },
                },
            )
            step_report_path = reports_path / f"balance_report.{step_id}.json"
            step_write = atomic_write_report(step_report_path, report)
            main_write = atomic_write_report(
                main_report_path,
                report,
                expected_existing_version_id=previous_version,
            )
            previous_version = main_write["version_id"]

        step_records.append(
            {
                "step_id": step_id,
                "step": step,
                "changed_keys": changed_keys,
                "active_input_dir": str(active_dir),
                "invalid_chunk_count": plan["invalid_chunk_count"],
                "direct_chunk_count": plan["direct_chunk_count"],
                "invalid_chunks": plan["invalid_chunks"],
                "reasons": plan["reasons"],
                "affected_battles": plan["affected_battles"],
                "consistency": {
                    "consistent": verification["consistent"],
                    "first_mismatch": verification["first_mismatch"],
                },
                "scheduler_round_count": len(schedule_result["rounds"]),
                "admission_count": len(schedule_result["admission_sequence"]),
                "step_report_path": step_write["path"],
                "main_report_version": main_write["version_id"],
            }
        )

    invalidation_report = {
        "schema_version": "invalidation-report/v1",
        "status": "completed",
        "all_consistent": all_consistent,
        "initial_consistency": initial["verification"],
        "steps": step_records,
        "total_steps": len(step_records),
        "total_invalid_chunks": sum(
            int(item["invalid_chunk_count"]) for item in step_records
        ),
        "main_report_path": str(main_report_path),
    }
    atomic_write_json(reports_path / "invalidation_report.json", invalidation_report)
    return invalidation_report
