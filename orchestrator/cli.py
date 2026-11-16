"""Command line entry point for the pipeline orchestrator."""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Optional

from .dag_loader import dag_fingerprint, load_dag
from .errors import OrchestratorError
from .replay import replay_snapshot
from .snapshot import (
    Snapshot,
    compare_version,
    load_snapshot,
    recover_completed,
    save_snapshot,
)
from .ui import launch_tk, render_headless
from .verifier import verify_snapshot


def _print(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    sys.stdout.write("\n")


def _resolve(path: str) -> str:
    return os.path.abspath(path)


def cmd_validate_dag(args) -> int:
    dag = load_dag(_resolve(args.dag))
    base_dir = os.path.dirname(_resolve(args.dag))
    _print(
        {
            "code": "OK",
            "dag_id": dag.dag_id,
            "dag_version": dag.dag_version,
            "stage_count": len(dag.stages),
            "topological_order": dag.topological_order(),
            "fingerprint": dag_fingerprint(dag, base_dir),
        }
    )
    return 0


def cmd_run(args) -> int:
    from .scheduler import Scheduler

    dag_path = _resolve(args.dag)
    dag = load_dag(dag_path)
    base_dir = os.path.dirname(dag_path)
    workspace = _resolve(args.workspace)
    os.makedirs(workspace, exist_ok=True)
    snapshot_path = _resolve(args.snapshot)
    failure_config = _parse_failure(args.fail_first_n)
    scheduler = Scheduler(
        dag=dag,
        base_dir=base_dir,
        workspace_dir=workspace,
        snapshot_path=snapshot_path,
        failure_config=failure_config,
    )
    snapshot = scheduler.run()
    report = verify_snapshot(snapshot, workspace)
    _print(
        {
            "code": "OK",
            "session_id": snapshot.session_id,
            "snapshot": snapshot_path,
            "states": {
                sid: result.state for sid, result in sorted(snapshot.states.items())
            },
            "verification": report.to_dict(),
        }
    )
    return 0


def _parse_failure(raw: Optional[str]) -> dict:
    if not raw:
        return {}
    config = {}
    for pair in raw.split(","):
        stage_id, value = pair.split("=", 1)
        config[stage_id.strip()] = int(value)
    return config


def cmd_verify(args) -> int:
    snapshot = load_snapshot(_resolve(args.snapshot))
    report = verify_snapshot(snapshot, _resolve(args.workspace))
    _print(report.to_dict())
    return 0 if report.ok else 3


def cmd_replay(args) -> int:
    snapshot = load_snapshot(_resolve(args.snapshot))
    dag_path = _resolve(args.dag)
    base_dir = os.path.dirname(dag_path)
    report = replay_snapshot(
        snapshot=snapshot,
        base_dir=base_dir,
        replay_root=_resolve(args.replay_root),
        dag_path=dag_path,
    )
    _print(report.to_dict())
    return 0 if report.ok else 4


def cmd_recover(args) -> int:
    snapshot_path = _resolve(args.snapshot)
    snapshot = load_snapshot(snapshot_path)
    dag = load_dag(_resolve(args.dag))
    base_dir = os.path.dirname(_resolve(args.dag))
    current_fingerprint = dag_fingerprint(dag, base_dir)
    decision = compare_version(snapshot, dag.dag_version, current_fingerprint)
    response = {
        "code": "OK",
        "decision": decision.action,
        "reasons": decision.reasons,
    }
    if decision.action == VersionDecisionReplace():
        replacement_path = snapshot_path + ".replaced"
        save_snapshot(replacement_path, snapshot)
        response["archived_snapshot"] = replacement_path
        response["policy"] = "full_replacement_no_incremental_merge"
        _print(response)
        return 0

    recovered = recover_completed(snapshot)
    response["recovered_completed_stages"] = sorted(recovered)
    unknown = sorted(
        sid for sid in dag.stage_ids_sorted() if sid not in recovered
    )
    response["unknown_stages_requiring_rerun"] = unknown
    response["exit_report_generated"] = False
    _print(response)
    return 0


def VersionDecisionReplace() -> str:
    from .snapshot import VersionDecision

    return VersionDecision.REPLACE


def cmd_ui(args) -> int:
    if args.headless:
        model = render_headless(_resolve(args.snapshot))
        _print(
            {
                "code": "OK",
                "title": model.title,
                "dag_version": model.dag_version,
                "budget": model.budget,
                "edges": model.edges,
                "stage_rows": [row.__dict__ for row in model.stage_rows],
                "timeline": [band.__dict__ for band in model.timeline],
                "waiting": model.waiting,
                "snapshots": model.snapshots,
            }
        )
        return 0
    return launch_tk(_resolve(args.snapshot))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m orchestrator.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("validate-dag", help="validate a DAG definition file")
    p.add_argument("--dag", required=True)
    p.set_defaults(func=cmd_validate_dag)

    p = sub.add_parser("run", help="run a pipeline session")
    p.add_argument("--dag", required=True)
    p.add_argument("--workspace", required=True)
    p.add_argument("--snapshot", required=True)
    p.add_argument("--fail-first-n", default="")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("verify", help="verify a snapshot against outputs")
    p.add_argument("--snapshot", required=True)
    p.add_argument("--workspace", required=True)
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("replay", help="replay completed stages after recovery")
    p.add_argument("--dag", required=True)
    p.add_argument("--snapshot", required=True)
    p.add_argument("--replay-root", required=True)
    p.set_defaults(func=cmd_replay)

    p = sub.add_parser("recover", help="inspect recovery/version policy")
    p.add_argument("--dag", required=True)
    p.add_argument("--snapshot", required=True)
    p.set_defaults(func=cmd_recover)

    p = sub.add_parser("ui", help="open the Tk dashboard or headless model")
    p.add_argument("--snapshot", required=True)
    p.add_argument("--headless", action="store_true")
    p.set_defaults(func=cmd_ui)

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except OrchestratorError as exc:
        _print(exc.to_dict())
        return {
            "E_DAG": 2,
            "E_VERIFY": 3,
            "E_REPLAY": 4,
            "E_SNAPSHOT": 5,
            "E_EXEC": 6,
        }.get(exc.code, 1)


if __name__ == "__main__":
    raise SystemExit(main())
