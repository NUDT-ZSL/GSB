from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from .engine import DiagnosticEngine, EngineConfig
from .parser import (
    load_function_specs,
    load_requests,
    load_v1_policy,
    load_v2_package,
)
from .policy import merge_policy_records
from .policy_store import PolicyPackageManager
from .sample_data import generate_sample
from .store import StateStore
from .utils import read_json, sha256_json


def load_sample_inputs(data_dir: str | Path = "data") -> dict[str, Any]:
    root = Path(data_dir)
    if not root.exists():
        generate_sample(root)
    functions = load_function_specs(root / "functions.json")
    function_names = [item.name for item in functions]
    requests = load_requests(root / "requests.json", function_names)
    v1_payload = read_json(root / "policies_v1.json")
    v1_records = load_v1_policy(root / "policies_v1.json")
    v2_records, v2_manifest, v2_metadata = load_v2_package(root / "policies_v2")
    policy_view = merge_policy_records(v1_records + v2_records)
    scenario = read_json(root / "scenario.json")
    return {
        "root": root,
        "functions": functions,
        "requests": requests,
        "v1_payload": v1_payload,
        "v1_records": v1_records,
        "v2_records": v2_records,
        "v2_manifest": v2_manifest,
        "v2_metadata": v2_metadata,
        "policy_view": policy_view,
        "scenario": scenario,
    }


def build_engine(
    data_dir: str | Path = "data",
    *,
    db_path: str | Path = ":memory:",
    store: StateStore | None = None,
    crash_before_commit: str | None = None,
) -> tuple[DiagnosticEngine, dict[str, Any]]:
    inputs = load_sample_inputs(data_dir)
    scenario = inputs["scenario"]
    config_data = scenario.get("engine", {})
    config = EngineConfig(**config_data)
    state_store = store or StateStore(db_path)
    if crash_before_commit:
        state_store.set_kill_before_commit(crash_before_commit)

    manager = PolicyPackageManager(
        state_store,
        dependencies={spec.name: spec.dependencies for spec in inputs["functions"]},
    )
    pointer = state_store.get_active_policy_snapshot()
    if pointer is None:
        manager.install_view_snapshot(
            snapshot_id="baseline-merged-v1-v2",
            version=1,
            clock_ms=0,
            policy_view=inputs["policy_view"],
            manifest={
                "kind": "merged_v1_v2",
                "v2_manifest_version": inputs["v2_manifest"].get("version"),
            },
        )
        pointer = state_store.get_active_policy_snapshot()

    engine = DiagnosticEngine(
        functions=inputs["functions"],
        requests=inputs["requests"],
        policy_view=inputs["policy_view"],
        config=config,
        store=state_store,
        policy_pointer=pointer,
    )
    return engine, inputs


def run_scenario(
    data_dir: str | Path = "data",
    *,
    db_path: str | Path = ":memory:",
    crash_before_commit: str | None = None,
    recovery_clock_ms: int = 0,
    engine: DiagnosticEngine | None = None,
    inputs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if engine is None:
        engine, inputs = build_engine(
            data_dir,
            db_path=db_path,
            crash_before_commit=crash_before_commit,
        )
    assert inputs is not None
    scenario = inputs["scenario"]
    recovery_report = engine.recover(recovery_clock_ms=recovery_clock_ms)

    step_order = 0

    def next_order() -> int:
        nonlocal step_order
        step_order += 1
        return step_order

    def run_round(round_spec: dict[str, Any]) -> None:
        round_id = str(round_spec["id"])
        step_id = f"round:{round_id}"
        order = next_order()
        clock_ms = int(round_spec["clock_ms"])
        if engine.store.is_step_committed(step_id):
            return
        digest = sha256_json({"round": round_spec})
        engine.store.prepare_step(step_id, order, digest, round_spec, clock_ms)
        decisions = engine.schedule_round(
            round_id,
            clock_ms,
            candidates=round_spec.get("candidates"),
            budget_ms=round_spec.get("budget_ms"),
            global_limit=round_spec.get("global_cold_start_limit"),
        )
        result = {"step_id": step_id, "decisions": decisions}
        engine.persist_step(
            step_id,
            order,
            clock_ms,
            result,
            input_digest=digest,
            planned=round_spec,
        )

    rounds_by_after_seq: dict[int, list[dict[str, Any]]] = {}
    for round_spec in scenario.get("rounds", []):
        rounds_by_after_seq.setdefault(int(round_spec.get("after_seq", 0)), []).append(
            round_spec
        )

    for round_spec in rounds_by_after_seq.get(0, []):
        run_round(round_spec)

    for request in sorted(inputs["requests"], key=lambda item: item.seq):
        step_id = f"request:{request.seq}"
        order = next_order()
        if not engine.store.is_step_committed(step_id):
            digest = sha256_json({"request": asdict(request)})
            engine.store.prepare_step(
                step_id,
                order,
                digest,
                {"seq": request.seq, "function_name": request.function_name},
                request.logical_clock_ms,
            )
            call = engine.process_request(request)
            engine.persist_step(
                step_id,
                order,
                request.logical_clock_ms,
                {"step_id": step_id, "call": call},
                input_digest=digest,
                planned={"seq": request.seq},
            )
        for round_spec in rounds_by_after_seq.get(request.seq, []):
            run_round(round_spec)

    horizon = int(scenario.get("horizon_ms", engine.logical_clock_watermark_ms))
    final_step = "finalize:horizon"
    order = next_order()
    if not engine.store.is_step_committed(final_step):
        digest = sha256_json({"horizon": horizon})
        engine.store.prepare_step(final_step, order, digest, {"horizon_ms": horizon}, horizon)
        engine.complete_until(horizon)
        result = {
            "step_id": final_step,
            "dispatch_report": engine.dispatch_report(),
            "state_digest": sha256_json(engine.state_dict()),
        }
        engine.persist_step(
            final_step,
            order,
            horizon,
            result,
            input_digest=digest,
            planned={"horizon_ms": horizon},
        )

    final_recovery_report = engine.recover(recovery_clock_ms=recovery_clock_ms)
    return {
        "engine": engine,
        "inputs": inputs,
        "recovery_report": recovery_report.to_dict(),
        "final_recovery_report": final_recovery_report.to_dict(),
        "state": engine.state_dict(),
        "dispatch_report": engine.dispatch_report(),
        "policy_view": {
            key: value.to_dict() for key, value in inputs["policy_view"].items()
        },
    }


def summarize_result(result: dict[str, Any]) -> dict[str, Any]:
    state = result["state"]
    decisions = state["decisions"]
    outcome_counts: dict[str, int] = {}
    for decision in decisions:
        outcome = decision["outcome"]
        outcome_counts[outcome] = outcome_counts.get(outcome, 0) + 1
    by_candidate: dict[str, list[dict[str, Any]]] = {}
    for decision in decisions:
        by_candidate.setdefault(decision["candidate_id"], []).append(decision)

    terminal_outcomes: list[dict[str, Any]] = []
    for candidate_id, candidate_decisions in sorted(by_candidate.items()):
        failure = next(
            (item for item in candidate_decisions if item["outcome"] == "warmup_failed"), None
        )
        abandoned = next(
            (item for item in reversed(candidate_decisions) if item["outcome"] == "abandoned"),
            None,
        )
        started = next(
            (item for item in candidate_decisions if item["outcome"] == "started"), None
        )
        held = next(
            (
                item
                for item in reversed(candidate_decisions)
                if item["outcome"] == "not_started_in_budget"
            ),
            None,
        )
        if failure is not None:
            terminal_outcomes.append(failure)
        elif abandoned is not None:
            terminal_outcomes.append(abandoned)
        elif started is not None:
            terminal_outcomes.append(started)
        elif held is not None:
            terminal_outcomes.append(held)

    phase_counts: dict[str, int] = {}
    for instance in state["instances"].values():
        phase_counts[instance["phase"]] = phase_counts.get(instance["phase"], 0) + 1

    return {
        "recovery_report": result["recovery_report"],
        "final_recovery_report": result.get("final_recovery_report", {}),
        "logical_clock_watermark_ms": state["logical_clock_watermark_ms"],
        "processed_request_seq": state["processed_request_seq"],
        "phase_counts": phase_counts,
        "decision_outcome_counts": outcome_counts,
        "candidate_outcomes": terminal_outcomes,
        "dispatch_report": result["dispatch_report"],
        "precision_markers": state["ledger"]["markers"],
        "ledger_totals": {
            "entries": len(state["ledger"]["entries"]),
            "cumulative_error_bound_ms": state["ledger"]["cumulative_error_bound_ms"],
            "cumulative_abs_rounding_error_ms": state["ledger"][
                "cumulative_abs_rounding_error_ms"
            ],
        },
        "policy_keys": sorted(result["policy_view"]),
    }
