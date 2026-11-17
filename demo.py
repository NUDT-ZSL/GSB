#!/usr/bin/env python3
"""Repeatable three-scenario loopback health-check demonstration."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lbhealth.behavior import behavior_digest, read_behavior, write_behavior  # noqa: E402
from lbhealth.config import Config, load_config  # noqa: E402
from lbhealth.consistency import validate_round  # noqa: E402
from lbhealth.engine import HealthController, HealthEngine  # noqa: E402
from lbhealth.ledger import Ledger  # noqa: E402
from lbhealth.models import RoundResult  # noqa: E402
from lbhealth.prober import LoopbackProber  # noqa: E402
from lbhealth.recovery import recover_session  # noqa: E402
from lbhealth.replay import ReplayVerifier  # noqa: E402
from lbhealth.snapshot import SnapshotService  # noqa: E402


@dataclass
class ManagedBackend:
    member_id: str
    protocol: str
    port: int
    behavior_path: Path
    ready_path: Path
    state_path: Path
    log_path: Path
    process: subprocess.Popen[bytes] | None = field(default=None, repr=False)

    def start(self, start_index: int = 0) -> None:
        for attempt in range(3):
            if self.ready_path.exists():
                self.ready_path.unlink()
            command = [
                sys.executable,
                str(ROOT / "scripts" / "backend_sim.py"),
                "--protocol",
                self.protocol,
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
                "--behavior",
                str(self.behavior_path),
                "--start-index",
                str(start_index),
                "--state-file",
                str(self.state_path),
                "--ready-file",
                str(self.ready_path),
            ]
            with self.log_path.open("ab") as log:
                self.process = subprocess.Popen(
                    command,
                    cwd=str(ROOT),
                    stdout=log,
                    stderr=subprocess.STDOUT,
                )
            deadline = time.monotonic() + 6.0
            while time.monotonic() < deadline:
                if self.ready_path.exists():
                    try:
                        ready = json.loads(self.ready_path.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        time.sleep(0.02)
                        continue
                    if ready.get("status") == "ready" and int(ready.get("port", 0)) == self.port:
                        return
                if self.process.poll() is not None:
                    break
                time.sleep(0.02)
            self.stop()
            time.sleep(0.15 * (attempt + 1))
        raise RuntimeError(f"backend {self.member_id} failed to become ready on port {self.port}")

    def stop(self) -> None:
        if self.process is None:
            return
        if self.process.poll() is None:
            self.process.kill()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        self.process = None


def _port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _choose_ports(start: int, end: int, count: int) -> list[int]:
    ports = [port for port in range(start, end + 1) if _port_available(port)]
    if len(ports) < count:
        raise RuntimeError(f"port range {start}-{end} does not contain {count} free loopback ports")
    return ports[:count]


def _round_report(config: Config, result: RoundResult) -> dict[str, Any]:
    check = validate_round(config, result)
    return {
        "round": result.round_no,
        "probes": [probe.deterministic() for probe in result.probes],
        "transitions": [transition.deterministic() for transition in result.transitions],
        "available_members": list(result.available_members),
        "effective_weights": {
            member.id: int(result.effective_weights.get(member.id, 0))
            for member in config.members
        },
        "effective_weight_sum": result.effective_weight_sum,
        "full_weight_sum": result.full_weight_sum,
        "member_runtime": [
            {
                "member": member.id,
                "state": result.state_after.member_states[member.id].state.value,
                "success_count": result.state_after.member_states[member.id].success_count,
                "failure_count": result.state_after.member_states[member.id].failure_count,
                "slow_start_index": result.state_after.member_states[member.id].slow_start_index,
            }
            for member in config.members
        ],
        "consistency": {
            "status": check["status"],
            "code": check["code"],
            "errors": check["errors"],
            "weight_formula": check["weight_formula"],
            "all_available_members_at_full_weight": check[
                "all_available_members_at_full_weight"
            ],
        },
    }


def _scenario_ok(rounds: list[dict[str, Any]]) -> bool:
    return all(round_item["consistency"]["status"] == "ok" for round_item in rounds)


def _manifest_for_context(context: dict[str, Any]) -> str:
    payload = {
        "members": context.get("members", {}),
        "refused_members": context.get("refused_members", []),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _context_behaviors(config: Config, start_round: int, end_round: int) -> dict[str, Any]:
    members: dict[str, Any] = {}
    for member in config.members:
        path = ROOT / "artifacts" / "run" / "behaviors" / f"{member.id}.json"
        if not path.exists():
            continue
        plan = read_behavior(path)
        members[member.id] = {
            "protocol": member.protocol,
            "start_index": start_round - 1,
            "behavior_by_round": {
                str(round_no): plan for round_no in range(start_round, end_round + 1)
            },
        }
    return {
        "kind": "loopback-deterministic-v1",
        "window_start_round": start_round,
        "window_end_round": end_round,
        "members": members,
        "refused_members": [member.id for member in config.members if member.id not in members],
    }


def _prepare_replay_behavior(
    context: dict[str, Any],
    *,
    negative_bravo_round: int | None = None,
    negative_round: int | None = None,
) -> Any:
    behavior_dir = ROOT / "artifacts" / "run" / "behaviors"

    def prepare(round_no: int, _snapshot: Any) -> None:
        for member_id, member_context in context.get("members", {}).items():
            plan = member_context["behavior_by_round"][str(round_no)]
            if (
                negative_round is not None
                and round_no == negative_round
                and member_id == "bravo"
                and negative_bravo_round == round_no
            ):
                plan = {
                    "version": 1,
                    "start_index": member_context["start_index"],
                    "default": "fail",
                    "actions": [],
                    "slow_ms": 0,
                }
            write_behavior(behavior_dir / f"{member_id}.json", plan)

    return prepare


def _build_backends(
    config: Config,
    port_map: dict[str, int],
    run_dir: Path,
) -> dict[str, ManagedBackend]:
    backends: dict[str, ManagedBackend] = {}
    for member in config.members:
        behavior_path = run_dir / "behaviors" / f"{member.id}.json"
        if not behavior_path.exists():
            continue
        backends[member.id] = ManagedBackend(
            member_id=member.id,
            protocol=member.protocol,
            port=port_map[member.id],
            behavior_path=behavior_path,
            ready_path=run_dir / "ready" / f"{member.id}.json",
            state_path=run_dir / "state" / f"{member.id}.json",
            log_path=run_dir / "logs" / f"{member.id}.log",
        )
    return backends


def _copy_initial_behaviors(run_dir: Path) -> None:
    mapping = {
        "alpha": "behavior_alpha_normal.json",
        "bravo": "behavior_bravo_intermittent.json",
        "charlie": "behavior_charlie_slow.json",
    }
    for member_id, source_name in mapping.items():
        source = ROOT / "config" / source_name
        target = run_dir / "behaviors" / f"{member_id}.json"
        target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")


def _render_config(port_map: dict[str, int], output: Path) -> Config:
    raw = json.loads((ROOT / "config" / "sample_pool.json").read_text(encoding="utf-8"))
    for member in raw["pool"]["members"]:
        member["port"] = port_map[member["id"]]
    output.write_text(json.dumps(raw, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return load_config(output)


def _wait_marker(marker: Path, process: subprocess.Popen[bytes], timeout: float = 12.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if marker.exists():
            return json.loads(marker.read_text(encoding="utf-8"))
        if process.poll() is not None:
            raise RuntimeError(f"crash worker exited before marker; return code {process.returncode}")
        time.sleep(0.02)
    raise RuntimeError("timed out waiting for the crash-test marker")


def _force_kill(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/PID", str(process.pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        process.kill()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def run_demo(port_start: int, port_end: int, keep_run: bool) -> dict[str, Any]:
    run_dir = ROOT / "artifacts" / "run"
    if run_dir.exists() and not keep_run:
        shutil.rmtree(run_dir)
    for name in ("behaviors", "ready", "state", "logs"):
        (run_dir / name).mkdir(parents=True, exist_ok=True)
    _copy_initial_behaviors(run_dir)

    ports = _choose_ports(port_start, port_end, 4)
    port_map = {
        "alpha": ports[0],
        "bravo": ports[1],
        "charlie": ports[2],
        "delta": ports[3],
    }
    config_path = run_dir / "config.json"
    config = _render_config(port_map, config_path)
    db_path = run_dir / "ledger.sqlite"
    for suffix in ("", "-wal", "-shm"):
        path = Path(str(db_path) + suffix)
        if path.exists():
            path.unlink()

    backends = _build_backends(config, port_map, run_dir)
    for backend in backends.values():
        backend.start(start_index=0)

    report: dict[str, Any] = {
        "status": "ok",
        "component": "lbhealth",
        "component_version": "1.0.0",
        "environment": {
            "transport": "loopback-only",
            "ports": port_map,
            "refused_member": "delta (intentionally no listener)",
        },
        "config": {
            "version": config.config_version,
            "member_digest": config.member_digest,
            "config_digest": config.config_digest,
            "full_weight_sum": config.pool.full_weight_sum,
            "slow_start_weight_table": list(config.pool.slow_start_weight_table),
        },
        "scenarios": [],
    }

    try:
        ledger = Ledger(db_path)
        session = recover_session(config, ledger)
        controller = HealthController(
            config, ledger, session.engine, LoopbackProber(config)
        )

        cold_results = controller.run_rounds(5, fixed_period=True)
        cold_rounds = [_round_report(config, result) for result in cold_results]
        cold_transitions = [
            transition
            for round_item in cold_rounds
            for transition in round_item["transitions"]
        ]
        report["scenarios"].append(
            {
                "name": "cold_start_convergence",
                "rounds_run": [result.round_no for result in cold_results],
                "rounds": cold_rounds,
                "transitions": cold_transitions,
                "conclusion": "OK" if _scenario_ok(cold_rounds) else "E_CONSISTENCY",
            }
        )

        injection = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "inject_behavior.py"),
                "--path",
                str(run_dir / "behaviors" / "alpha.json"),
                "--plan-file",
                str(ROOT / "config" / "behavior_alpha_fault.json"),
            ],
            cwd=str(ROOT),
            check=True,
            capture_output=True,
            text=True,
        )
        injection_report = json.loads(injection.stdout)
        fault_results = controller.run_rounds(5, fixed_period=True)
        fault_rounds = [_round_report(config, result) for result in fault_results]
        fault_transitions = [
            transition
            for round_item in fault_rounds
            for transition in round_item["transitions"]
        ]
        report["scenarios"].append(
            {
                "name": "fault_injection_transitions",
                "injection": {
                    "member": "alpha",
                    "behavior_sha256": injection_report["sha256"],
                    "plan": injection_report["behavior"],
                },
                "rounds_run": [result.round_no for result in fault_results],
                "rounds": fault_rounds,
                "transitions": fault_transitions,
                "conclusion": (
                    "OK"
                    if _scenario_ok(fault_rounds)
                    and any(
                        item["member"] == "alpha" and item["to"] == "unhealthy"
                        for item in fault_transitions
                    )
                    else "E_TRANSITION"
                ),
            }
        )

        marker = run_dir / "crash-marker.json"
        if marker.exists():
            marker.unlink()
        crash_process = subprocess.Popen(
            [
                sys.executable,
                str(ROOT / "scripts" / "crash_advance.py"),
                "--config",
                str(config_path),
                "--db",
                str(db_path),
                "--marker",
                str(marker),
                "--pause-seconds",
                "60",
            ],
            cwd=str(ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        marker_payload = _wait_marker(marker, crash_process)
        _force_kill(crash_process)

        recovered = recover_session(config, ledger)
        recovered_engine = recovered.engine
        controller_after = HealthController(
            config, ledger, recovered_engine, LoopbackProber(config)
        )
        recovered_results = controller_after.run_rounds(4, fixed_period=True)
        recovered_rounds = [_round_report(config, result) for result in recovered_results]
        recovered_transitions = [
            transition
            for round_item in recovered_rounds
            for transition in round_item["transitions"]
        ]

        snapshot_service = SnapshotService(ledger)
        context = _context_behaviors(config, 11, 14)
        context["behavior_manifest"] = _manifest_for_context(context)
        snapshot = snapshot_service.create(
            config,
            recovered_engine,
            label="post-recovery-replay-window",
            window=4,
            replay_context=context,
        )
        restore = snapshot_service.restore(
            snapshot.snapshot_id,
            config,
            previous_state=recovered_engine.snapshot_state(),
        )

        for backend in backends.values():
            backend.stop()
        time.sleep(0.15)

        replay_backends = _build_backends(config, port_map, run_dir)
        for member_id, backend in replay_backends.items():
            start_index = int(context["members"][member_id]["start_index"])
            first_plan = context["members"][member_id]["behavior_by_round"]["11"]
            write_behavior(backend.behavior_path, first_plan)
            backend.start(start_index=start_index)

        negative_prepare = _prepare_replay_behavior(
            context, negative_bravo_round=11, negative_round=11
        )
        negative_report = ReplayVerifier(config).verify(
            snapshot,
            prepare_round=negative_prepare,
        )

        for backend in replay_backends.values():
            backend.stop()
        time.sleep(0.15)
        for member_id, backend in replay_backends.items():
            start_index = int(context["members"][member_id]["start_index"])
            first_plan = context["members"][member_id]["behavior_by_round"]["11"]
            write_behavior(backend.behavior_path, first_plan)
            backend.start(start_index=start_index)
        positive_report = ReplayVerifier(config).verify(
            snapshot,
            prepare_round=_prepare_replay_behavior(context),
            behavior_manifest=context["behavior_manifest"],
        )

        positive_ok = positive_report.ok
        negative_ok = (
            negative_report.code == "E_REPLAY"
            and negative_report.round_no == 11
            and negative_report.member_id == "bravo"
            and negative_report.field == "probe.ok"
        )
        replay_ok = positive_ok and negative_ok
        recovery_ok = (
            recovered.report.requires_rerun
            and any(item["round"] == 11 for item in recovered.report.unknown_rounds)
            and [result.round_no for result in recovered_results] == [11, 12, 13, 14]
            and _scenario_ok(recovered_rounds)
        )
        report["scenarios"].append(
            {
                "name": "forced_termination_recovery_replay",
                "crash_marker": marker_payload,
                "recovery": recovered.report.to_dict(),
                "rounds_run": [result.round_no for result in recovered_results],
                "rounds": recovered_rounds,
                "transitions": recovered_transitions,
                "snapshot": snapshot.summary(),
                "snapshot_replay_context": {
                    "window": [11, 14],
                    "start_indices": {
                        member_id: data["start_index"]
                        for member_id, data in context["members"].items()
                    },
                    "behavior_manifest": context["behavior_manifest"],
                },
                "snapshot_restore": restore.to_dict(),
                "negative_replay": negative_report.to_dict(),
                "positive_replay": positive_report.to_dict(),
                "conclusion": "OK" if recovery_ok and replay_ok else "E_RECOVERY_REPLAY",
            }
        )
    finally:
        for backend in locals().get("backends", {}).values():
            backend.stop()
        for backend in locals().get("replay_backends", {}).values():
            backend.stop()

    report["final_consistency"] = {
        "status": "ok"
        if all(scenario["conclusion"] == "OK" for scenario in report["scenarios"])
        else "error",
        "scenarios_ok": sum(1 for scenario in report["scenarios"] if scenario["conclusion"] == "OK"),
        "scenarios_total": len(report["scenarios"]),
        "ledger": str(db_path.relative_to(ROOT)),
        "config": str(config_path.relative_to(ROOT)),
    }
    deterministic_core = [
        {
            "name": scenario["name"],
            "rounds_run": scenario["rounds_run"],
            "rounds": scenario["rounds"],
            "transitions": scenario.get("transitions", []),
            "conclusion": scenario["conclusion"],
        }
        for scenario in report["scenarios"]
    ]
    report["final_consistency"]["deterministic_digest"] = hashlib.sha256(
        json.dumps(
            deterministic_core,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port-start", type=int, default=19461)
    parser.add_argument("--port-end", type=int, default=19520)
    parser.add_argument("--keep-run", action="store_true")
    parser.add_argument(
        "--output", default=str(ROOT / "artifacts" / "demo_report.json")
    )
    args = parser.parse_args(argv)
    if not (1 <= args.port_start <= args.port_end <= 65535):
        parser.error("require 1 <= --port-start <= --port-end <= 65535")
    report = run_demo(args.port_start, args.port_end, args.keep_run)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    summary = {
        "status": report["final_consistency"]["status"],
        "report": str(output),
        "scenarios": [
            {
                "name": scenario["name"],
                "rounds": scenario["rounds_run"],
                "conclusion": scenario["conclusion"],
            }
            for scenario in report["scenarios"]
        ],
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if report["final_consistency"]["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
