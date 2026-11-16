"""Repeatable, fully local demonstration of the pipeline orchestrator.

Scenarios, executed in a fixed order:
  1. full successful run + verification
  2. failure injection, retries, skip propagation + verification
  3. hard-stop recovery, completed-only restore, replay verification
  4. negative controls: cyclic DAG (E_DAG), tampered snapshot (E_VERIFY),
     version replacement (E_REPLAY/REPLACE), headless UI model

All reports are rendered with sorted keys for stable output.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from typing import Dict

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from orchestrator.cli import main as cli_main  # noqa: E402
from orchestrator.dag_loader import dag_fingerprint, load_dag  # noqa: E402
from orchestrator.replay import replay_snapshot  # noqa: E402
from orchestrator.scheduler import Scheduler  # noqa: E402
from orchestrator.snapshot import (  # noqa: E402
    VersionDecision,
    compare_version,
    load_snapshot,
    recover_completed,
    save_snapshot,
)
from orchestrator.ui import render_headless  # noqa: E402
from orchestrator.verifier import verify_snapshot  # noqa: E402

RUNTIME = os.path.join(ROOT, "runtime")
SESSIONS = os.path.join(RUNTIME, "sessions")
WORK = os.path.join(RUNTIME, "work")
REPLAY = os.path.join(RUNTIME, "replay")


def reset_runtime() -> None:
    for path in (RUNTIME,):
        shutil.rmtree(path, ignore_errors=True)
    os.makedirs(SESSIONS, exist_ok=True)


def banner(title: str) -> None:
    line = "=" * 78
    print("\n" + line)
    print(title)
    print(line)


def emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def run_cli(*argv: str) -> int:
    return cli_main(list(argv))


def scenario_one_success() -> None:
    banner("SCENARIO 1: full successful run + independent verification")
    snapshot = os.path.join(SESSIONS, "scenario1.json")
    workspace = os.path.join(WORK, "scenario1")
    code = run_cli(
        "run",
        "--dag",
        "samples/dags/pipeline_valid.json",
        "--workspace",
        workspace,
        "--snapshot",
        snapshot,
    )
    print("run exit code:", code)
    verify_code = run_cli(
        "verify", "--snapshot", snapshot, "--workspace", workspace
    )
    print("verify exit code:", verify_code)


def scenario_two_failure() -> None:
    banner("SCENARIO 2: injected failure, retries, weak/strong propagation")
    snapshot = os.path.join(SESSIONS, "scenario2.json")
    workspace = os.path.join(WORK, "scenario2")
    code = run_cli(
        "run",
        "--dag",
        "samples/dags/pipeline_failure.json",
        "--workspace",
        workspace,
        "--snapshot",
        snapshot,
        "--fail-first-n",
        "validate=1,aggregate=3",
    )
    print("run exit code:", code)
    data = load_snapshot(snapshot)
    summary = {
        sid: {
            "state": result.state,
            "attempts": len(result.attempts),
            "missing_upstreams": result.missing_upstreams,
            "skipped_by": result.skipped_by,
        }
        for sid, result in sorted(data.states.items())
    }
    emit({"scenario": 2, "stage_outcomes": summary})
    verify_code = run_cli(
        "verify", "--snapshot", snapshot, "--workspace", workspace
    )
    print("verify exit code:", verify_code)


def scenario_three_recovery_replay() -> None:
    banner("SCENARIO 3: hard-stop recovery, restore, deterministic replay")
    dag_path = os.path.join(ROOT, "samples/dags/pipeline_valid.json")
    dag = load_dag(dag_path)
    base_dir = os.path.dirname(dag_path)
    snapshot_path = os.path.join(SESSIONS, "scenario3.json")
    workspace = os.path.join(WORK, "scenario3")

    scheduler = Scheduler(
        dag=dag,
        base_dir=base_dir,
        workspace_dir=workspace,
        snapshot_path=snapshot_path,
    )
    snapshot = scheduler.run()

    reopened = load_snapshot(snapshot_path)
    fingerprint = dag_fingerprint(dag, base_dir)
    decision = compare_version(reopened, dag.dag_version, fingerprint)
    recovered = recover_completed(reopened)
    unknown = scheduler.mark_unknown_after_crash(reopened)
    emit(
        {
            "scenario": 3,
            "version_decision": decision.action,
            "recovered_completed_stages": sorted(recovered),
            "unknown_after_hard_stop": unknown,
            "exit_report_generated": False,
        }
    )

    replay_root = os.path.join(REPLAY, "scenario3")
    report = replay_snapshot(
        snapshot=snapshot,
        base_dir=base_dir,
        replay_root=replay_root,
        dag_path=dag_path,
    )
    emit({"scenario": 3, "replay": report.to_dict()})


def scenario_four_negative_controls() -> None:
    banner("SCENARIO 4: tight-budget waiting queue + negative controls")
    print("\n[4a] tight budget must record waiting events without exceeding budget")
    tight_snapshot = os.path.join(SESSIONS, "scenario4-tight.json")
    tight_workspace = os.path.join(WORK, "scenario4-tight")
    code = run_cli(
        "run",
        "--dag",
        "samples/dags/pipeline_tight_budget.json",
        "--workspace",
        tight_workspace,
        "--snapshot",
        tight_snapshot,
    )
    print("tight run exit code:", code)
    tight_model = render_headless(tight_snapshot)
    emit(
        {
            "waiting_events": [
                {
                    "stage_id": event["stage_id"],
                    "reason": event["reason"],
                    "start_mono_ms": event["start_mono_ms"],
                }
                for event in tight_model.waiting
            ],
            "resource_curve_peak": max(
                (point["used"] for point in tight_model.resource_curve),
                default=0,
            ),
            "budget": tight_model.budget,
        }
    )

    print("\n[4b] cyclic DAG must be rejected with E_DAG")
    code = run_cli(
        "validate-dag", "--dag", "samples/dags/pipeline_cycle.json"
    )
    print("validate-dag exit code:", code)

    print("\n[4c] tampered output must be reported with E_VERIFY")
    snapshot_path = os.path.join(SESSIONS, "scenario1.json")
    workspace = os.path.join(WORK, "scenario1")
    target = os.path.join(workspace, "outputs", "report.jsonl")
    with open(target, "a", encoding="utf-8", newline="\n") as handle:
        handle.write('{"tampered": true}\n')
    code = run_cli(
        "verify", "--snapshot", snapshot_path, "--workspace", workspace
    )
    print("verify exit code:", code)

    print("\n[4d] DAG version drift must trigger full replacement")
    snapshot = load_snapshot(snapshot_path)
    decision = compare_version(snapshot, "9.9.9-drift", snapshot.dag_fingerprint)
    emit(
        {
            "decision": decision.action,
            "reasons": decision.reasons,
            "policy": "full_replacement_no_incremental_merge",
        }
    )

    print("\n[4e] headless UI view model (no Tk display required)")
    model = render_headless(snapshot_path)
    emit(
        {
            "title": model.title,
            "edge_count": len(model.edges),
            "stages": [row.stage_id for row in model.stage_rows],
            "timeline_bands": len(model.timeline),
            "waiting_events": len(model.waiting),
        }
    )


def main() -> int:
    reset_runtime()
    scenario_one_success()
    scenario_two_failure()
    scenario_three_recovery_replay()
    scenario_four_negative_controls()
    banner("DEMO COMPLETE")
    print("All scenarios executed. Snapshot directory:", SESSIONS)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
