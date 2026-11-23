#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import secrets
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scaler.loader import load_bundle
from scaler.models import canonical_json
from scaler.storage import SQLiteStore
from scaler.stub import StubServer, load_scenario


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Force-kill a control-plane round, recover, and compare snapshots"
    )
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--fault-round", type=int, default=2)
    parser.add_argument("--fault-after-dispatch", type=int, default=2)
    parser.add_argument("--services", default="samples/services.json")
    parser.add_argument("--nodes", default="samples/nodes.json")
    parser.add_argument("--trace", default="samples/trace.json")
    parser.add_argument("--config", default="samples/config.json")
    parser.add_argument("--scenario", default="samples/scenario.json")
    parser.add_argument("--output-dir", default="artifacts/chaos-drill")
    parser.add_argument("--run-id", default=None)
    parser.add_argument(
        "--fast-delays",
        action="store_true",
        help="keep logical budgets unchanged but shorten stub wall-clock sleeps",
    )
    return parser.parse_args()


def cli(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "scaler", *args],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
    )


def state_digest(state: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(state).encode("utf-8")).hexdigest()


def main() -> int:
    args = parse_args()
    if args.rounds < args.fault_round or args.fault_round < 1:
        raise SystemExit("--fault-round must be between 1 and --rounds")
    run_id = args.run_id or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    output_dir = (ROOT / args.output_dir / run_id).resolve()
    if output_dir.exists() and any(output_dir.iterdir()):
        output_dir = output_dir.with_name(f"{output_dir.name}-{secrets.token_hex(3)}")
    output_dir.mkdir(parents=True, exist_ok=True)

    spec = load_bundle(
        ROOT / args.services,
        ROOT / args.nodes,
        ROOT / args.trace,
        ROOT / args.config,
    )
    scenario = load_scenario(ROOT / args.scenario)
    if args.fast_delays:
        scenario = copy.deepcopy(scenario)
        for behavior in scenario.get("services", {}).values():
            behavior["delay_ms"] = 2
            if "then" in behavior:
                behavior["then"]["delay_ms"] = 2

    with StubServer(port=0, scenario=scenario) as stub:
        config_raw = spec.to_dict()["config"]
        config_raw["launch_target_url"] = stub.url
        config_path = output_dir / "config.json"
        config_path.write_text(
            json.dumps(config_raw, sort_keys=True, indent=2), encoding="utf-8"
        )
        common = [
            "--services", str(ROOT / args.services),
            "--nodes", str(ROOT / args.nodes),
            "--trace", str(ROOT / args.trace),
            "--config", str(config_path),
        ]
        env = dict(os.environ)
        env["PYTHONPATH"] = str(ROOT)

        baseline_db = output_dir / "baseline.sqlite"
        recovered_db = output_dir / "recovered.sqlite"

        baseline = cli(
            [
                "run",
                "--db",
                str(baseline_db),
                *common,
                "--rounds",
                str(args.rounds),
            ],
            env,
        )
        if baseline.returncode != 0:
            print(baseline.stdout)
            print(baseline.stderr, file=sys.stderr)
            return 1

        with stub.state.lock:
            stub.state.operations.clear()
            stub.state.service_attempts.clear()

        crashed = cli(
            [
                "run",
                "--db",
                str(recovered_db),
                *common,
                "--rounds",
                str(args.rounds),
                "--fault-round",
                str(args.fault_round),
                "--fault-after-dispatch",
                str(args.fault_after_dispatch),
            ],
            env,
        )
        if crashed.returncode not in {-9, 137}:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "reason": "controller was not force-terminated",
                        "returncode": crashed.returncode,
                        "stdout": crashed.stdout,
                        "stderr": crashed.stderr,
                    },
                    indent=2,
                    ensure_ascii=False,
                )
            )
            return 1

        crash_store = SQLiteStore(recovered_db)
        pending_state, pending_snapshot = crash_store.load()
        pending_rounds = pending_state.get("pending_rounds", {})
        if not pending_rounds:
            print("Expected an in-flight round after forced termination")
            return 1
        pending_round = pending_rounds[min(pending_rounds, key=int)]

        recovery = cli(["recover", "--db", str(recovered_db)], env)
        if recovery.returncode != 0:
            print(recovery.stdout)
            print(recovery.stderr, file=sys.stderr)
            return 1

        state_after_recovery, _ = crash_store.load()
        remaining_rounds = args.rounds - int(state_after_recovery["round_watermark"])
        continuation = None
        if remaining_rounds > 0:
            continuation = cli(
                [
                    "run",
                    "--db",
                    str(recovered_db),
                    *common,
                    "--rounds",
                    str(remaining_rounds),
                ],
                env,
            )
            if continuation.returncode != 0:
                print(continuation.stdout)
                print(continuation.stderr, file=sys.stderr)
                return 1

        baseline_state, baseline_snapshot = SQLiteStore(baseline_db).load()
        recovered_state, recovered_snapshot = crash_store.load()
        baseline_digest = state_digest(baseline_state)
        recovered_digest = state_digest(recovered_state)
        equal = baseline_state == recovered_state
        recovery_audit = crash_store.read_recovery_audit()
        recovery_event = [
            event
            for event in crash_store.read_events()
            if event["kind"] == "recovery.applied"
        ]

        (output_dir / "baseline_state.json").write_text(
            json.dumps(baseline_state, sort_keys=True, indent=2), encoding="utf-8"
        )
        (output_dir / "recovered_state.json").write_text(
            json.dumps(recovered_state, sort_keys=True, indent=2), encoding="utf-8"
        )
        report = {
            "ok": equal,
            "run_id": output_dir.name,
            "stub_url": stub.url,
            "rounds": args.rounds,
            "fault_round": args.fault_round,
            "fault_after_dispatch": args.fault_after_dispatch,
            "controller_exit": crashed.returncode,
            "pending_snapshot_id": pending_snapshot,
            "pending_round": {
                "round_no": pending_round["round_no"],
                "logical_clock_ms": pending_round["logical_clock_ms"],
                "budget": pending_round["budget"],
                "operation_count": len(pending_round["operations"]),
                "statuses": {
                    operation_id: operation["status"]
                    for operation_id, operation in sorted(
                        pending_round["operations"].items()
                    )
                },
            },
            "baseline_snapshot_id": baseline_snapshot,
            "recovered_snapshot_id": recovered_snapshot,
            "baseline_state_digest": baseline_digest,
            "recovered_state_digest": recovered_digest,
            "state_equal": equal,
            "round_watermark": recovered_state["round_watermark"],
            "state_revision": recovered_state["state_revision"],
            "pending_rounds_after_recovery": len(recovered_state["pending_rounds"]),
            "resumed_rounds": remaining_rounds,
            "ledger": recovered_state["nodes"],
            "recovery_audit": recovery_audit,
            "recovery_events": recovery_event,
            "artifacts": {
                "baseline_db": str(baseline_db),
                "recovered_db": str(recovered_db),
                "baseline_state": str(output_dir / "baseline_state.json"),
                "recovered_state": str(output_dir / "recovered_state.json"),
            },
        }
        report_path = output_dir / "report.json"
        report_path.write_text(
            json.dumps(report, sort_keys=True, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(json.dumps(report, sort_keys=True, ensure_ascii=False))
        return 0 if equal and not recovered_state["pending_rounds"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
