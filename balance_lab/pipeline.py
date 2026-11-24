from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from . import SCHEMA_VERSION
from .aggregate import aggregate_battle, deep_compare
from .canonical import digest_value
from .engine import simulate_all_chunks
from .ledger import Ledger
from .model import Model, load_model
from .report import atomic_write_report, make_report_version_id
from .scheduler import BudgetScheduler, LogicalClock, ManualLogicalClock


def verify_ledger_consistency(model: Model, ledger: Ledger) -> dict[str, Any]:
    abs_tol = float(model.config["consistency_abs_tolerance"])
    rel_tol = float(model.config["consistency_relative_tolerance"])
    battles: list[dict[str, Any]] = []
    consistent = True
    first_mismatch: dict[str, Any] | None = None

    for battle in model.battle_list():
        incremental = ledger.load_chunk_results(battle.battle_id)
        full = simulate_all_chunks(model, battle)
        battle_result: dict[str, Any] = {
            "battle_id": battle.battle_id,
            "incremental_chunks": len(incremental),
            "full_chunks": len(full),
            "consistent": True,
        }
        if len(incremental) != len(full):
            mismatch = {
                "battle_id": battle.battle_id,
                "round": None,
                "reason": "chunk_count_mismatch",
                "expected": len(full),
                "actual": len(incremental),
            }
            battle_result["consistent"] = False
            battle_result["first_mismatch"] = mismatch
            consistent = False
            if first_mismatch is None:
                first_mismatch = mismatch
            battles.append(battle_result)
            continue

        for expected_chunk, actual_chunk in zip(full, incremental):
            mismatch = deep_compare(
                expected_chunk,
                actual_chunk,
                abs_tolerance=abs_tol,
                relative_tolerance=rel_tol,
            )
            if mismatch is not None:
                mismatch["battle_id"] = battle.battle_id
                battle_result["consistent"] = False
                battle_result["first_mismatch"] = mismatch
                consistent = False
                if first_mismatch is None:
                    first_mismatch = mismatch
                break

        if battle_result["consistent"]:
            incremental_aggregate = aggregate_battle(model, battle, incremental)
            full_aggregate = aggregate_battle(model, battle, full)
            aggregate_mismatch = deep_compare(
                full_aggregate,
                incremental_aggregate,
                abs_tolerance=abs_tol,
                relative_tolerance=rel_tol,
            )
            if aggregate_mismatch is not None:
                aggregate_mismatch["battle_id"] = battle.battle_id
                battle_result["consistent"] = False
                battle_result["first_mismatch"] = aggregate_mismatch
                consistent = False
                if first_mismatch is None:
                    first_mismatch = aggregate_mismatch
        battles.append(battle_result)

    return {
        "consistent": consistent,
        "first_mismatch": first_mismatch,
        "abs_tolerance": abs_tol,
        "relative_tolerance": rel_tol,
        "battles": battles,
    }


def build_report(
    model: Model,
    schedule_result: dict[str, Any],
    aggregates: list[dict[str, Any]],
    *,
    revision: int,
    recovery: list[dict[str, Any]] | None = None,
    verification: dict[str, Any] | None = None,
    invalidation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    balanced = all(item["balanced"] for item in aggregates)
    aggregate_digests = [digest_value(item) for item in aggregates]
    version_id = make_report_version_id(
        model.input_version_id, revision, aggregate_digests
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "version_id": version_id,
        "revision": revision,
        "input_version_id": model.input_version_id,
        "balanced": balanced,
        "battle_count": len(aggregates),
        "balanced_battle_count": sum(1 for item in aggregates if item["balanced"]),
        "recovery": {"pruned_invalid_rows": recovery or []},
        "scheduler": {
            "status": schedule_result["status"],
            "parallelism": schedule_result["parallelism"],
            "time_budget_ms": schedule_result["time_budget_ms"],
            "memory_budget_bytes": schedule_result["memory_budget_bytes"],
            "round_count": len(schedule_result["rounds"]),
            "rounds": schedule_result["rounds"],
            "admission_sequence": schedule_result["admission_sequence"],
        },
        "battles": aggregates,
        "verification": verification,
        "invalidation": invalidation,
    }


def aggregate_completed_battles(
    model: Model, ledger: Ledger
) -> list[dict[str, Any]]:
    aggregates = []
    for battle in model.battle_list():
        results = ledger.load_chunk_results(battle.battle_id)
        aggregate = aggregate_battle(model, battle, results)
        ledger.upsert_battle_result(aggregate)
        aggregates.append(aggregate)
    return aggregates


def run_pipeline(
    input_dir: str | Path,
    state_dir: str | Path,
    report_path: str | Path | None = None,
    *,
    stop_event: threading.Event | None = None,
    clock: LogicalClock | None = None,
    revision: int = 0,
    expected_report_version_id: str | None = None,
    verify: bool = False,
    config_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    model = load_model(input_dir)
    if config_overrides:
        for key, value in config_overrides.items():
            if value is not None:
                model.config[key] = value
    state_path = Path(state_dir)
    state_path.mkdir(parents=True, exist_ok=True)
    with Ledger(state_path / "balance.db") as ledger:
        recovery = ledger.prune_invalid_chunks()
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
                "model": model,
                "schedule": schedule_result,
                "recovery": recovery,
            }

        aggregates = aggregate_completed_battles(model, ledger)
        verification = verify_ledger_consistency(model, ledger) if verify else None
        report = build_report(
            model,
            schedule_result,
            aggregates,
            revision=revision,
            recovery=recovery,
            verification=verification,
        )
        write_info = None
        if report_path is not None:
            write_info = atomic_write_report(
                report_path,
                report,
                expected_existing_version_id=expected_report_version_id,
            )
        marker = state_path / "interrupt.json"
        if marker.exists():
            marker.unlink()
        return {
            "status": "completed",
            "model": model,
            "schedule": schedule_result,
            "aggregates": aggregates,
            "report": report,
            "report_write": write_info,
            "verification": verification,
            "recovery": recovery,
        }
