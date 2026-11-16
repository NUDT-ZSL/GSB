"""Command-line entry point with fault-injection flags.

Injection changes only inputs / startup parameters - never source code:
  --inject-corrupt-scene   hand the loader a corrupted scene path (E_SCENE)
  --inject-port-busy       pre-occupy the loopback port before server start
                           (E_PORT)
  --inject-clock-rollback  configure the monotonic clock with a backwards jump
                           (E_CLOCK)
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import threading

from .errors import BenchError, StructuredLogger
from .clock import MonotonicClock
from .mock_server import MockServer, ServerConfig, ensure_port_free
from .scene import load_scene
from .session import SessionRunner
from .snapshot import SnapshotStore
from .versioning import VersionStore
from .replay import replay_snapshot
from .driver import request_stop_after


def _default_workspace() -> str:
    return os.path.join(os.getcwd(), "workspace")


def _paths(workspace: str) -> tuple[str, str, str, str]:
    snap_dir = os.path.join(workspace, "snapshots")
    state_dir = os.path.join(workspace, "state")
    log_dir = os.path.join(workspace, "logs")
    os.makedirs(snap_dir, exist_ok=True)
    os.makedirs(state_dir, exist_ok=True)
    os.makedirs(log_dir, exist_ok=True)
    return (snap_dir,
            os.path.join(state_dir, "versions.json"),
            os.path.join(log_dir, "events.jsonl"),
            os.path.join(log_dir, "server.log.jsonl"))


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True))


def _report_to_dict(report) -> dict:
    return {
        "snapshot_name": report.snapshot_name,
        "params_version": report.params_version,
        "interrupted": report.interrupted,
        "unknown_stage_indexes": report.unknown_stage_indexes,
        "completed_stage_indexes": [r.stage_index for r in report.completed],
        "verdicts": [
            {"stage_index": r.stage_index, "verdict": r.verdict,
             "saturation_reason": r.saturation_reason,
             "success_rate_bp": r.metrics.success_rate_bp,
             "p95_latency_ms": r.metrics.p95_latency_ms,
             "throughput_rps": r.metrics.throughput_rps}
            for r in report.completed
        ],
        "replacement": None if report.replacement is None else {
            "fully_replaced": report.replacement.fully_replaced,
            "replaced_stage_indexes": report.replacement.replaced_stage_indexes,
            "old_version": report.replacement.old_version,
            "new_version": report.replacement.new_version,
            "diffs": report.replacement.diffs,
        },
    }


def cmd_run(args: argparse.Namespace) -> int:
    workspace = args.workspace
    snap_dir, version_path, log_path, server_log = _paths(workspace)
    logger = StructuredLogger(log_path)
    try:
        scene_path = args.scene
        if args.inject_corrupt_scene:
            scene_path = _write_corrupt_scene(workspace)
        scene = load_scene(scene_path)

        server_cfg = ServerConfig(
            host=scene.target.host, port=scene.target.port,
            process_ms=args.process_ms, error_rate_bp=args.error_rate_bp,
            seed=args.seed, log_path=server_log)
        if args.inject_port_busy:
            holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            holder.bind((scene.target.host, scene.target.port))
            holder.listen(1)
            logger.log("WARN", "fault_injection", kind="port_busy",
                       address=scene.target.address)
        ensure_port_free(scene.target.host, scene.target.port)

        clock_factory = (lambda: MonotonicClock(
            rollback_ms=args.clock_rollback_ms,
            rollback_at_tick=args.clock_rollback_at_tick)) \
            if args.inject_clock_rollback else MonotonicClock

        stop_event = threading.Event()
        if args.force_stop_after_s:
            request_stop_after(stop_event, args.force_stop_after_s)
            logger.log("WARN", "fault_injection", kind="forced_termination",
                       after_s=args.force_stop_after_s)

        with MockServer(server_cfg):
            store = SnapshotStore(snap_dir)
            versions = VersionStore(version_path)
            runner = SessionRunner(
                store, versions,
                server_config={
                    "host": server_cfg.host, "port": server_cfg.port,
                    "process_ms": server_cfg.process_ms,
                    "error_rate_bp": server_cfg.error_rate_bp,
                    "seed": server_cfg.seed,
                },
                clock_factory=clock_factory)
            report = runner.run(args.snapshot_name, scene, stop_event)
        result = _report_to_dict(report)
        result["snapshot_path"] = store.path_of(args.snapshot_name)
        _print(result)
        logger.log("INFO", "run_finished", **result)
        return 0
    except BenchError as exc:
        logger.error(exc)
        _print({"error": exc.to_log_record()})
        return 2


def cmd_recover(args: argparse.Namespace) -> int:
    snap_dir, _, log_path, _ = _paths(args.workspace)
    logger = StructuredLogger(log_path)
    try:
        store = SnapshotStore(snap_dir)
        runner = SessionRunner(store, VersionStore(os.path.join(
            args.workspace, "state", "versions.json")), server_config={})
        snapshot = runner.recover(args.snapshot_name)
        plan = SessionRunner.recovery_plan(snapshot)
        _print(plan)
        logger.log("INFO", "recovered", **plan)
        return 0
    except BenchError as exc:
        logger.error(exc)
        _print({"error": exc.to_log_record()})
        return 2


def cmd_replay(args: argparse.Namespace) -> int:
    workspace = args.workspace
    snap_dir, _, log_path, server_log = _paths(workspace)
    logger = StructuredLogger(log_path)
    try:
        store = SnapshotStore(snap_dir)
        snapshot = store.load(args.snapshot_name)
        live_cfg = dict(snapshot.server_config)
        live_cfg["log_path"] = server_log
        if args.inject_port_busy:
            holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            holder.bind((live_cfg["host"], live_cfg["port"]))
            holder.listen(1)
        with MockServer(ServerConfig(**live_cfg)):
            report = replay_snapshot(snapshot, dict(snapshot.server_config))
        payload = {
            "snapshot_name": report.snapshot_name,
            "reproducible": report.reproducible,
            "tolerance": report.tolerance,
            "stages": [
                {"stage_index": item.stage_index, "matched": item.matched,
                 "observed": item.observed, "expected": item.expected}
                for item in report.items
            ],
        }
        _print(payload)
        logger.log("INFO", "replay_finished", **payload)
        return 0
    except BenchError as exc:
        logger.error(exc)
        _print({"error": exc.to_log_record()})
        return 2


def cmd_gui(args: argparse.Namespace) -> int:
    snap_dir, _, log_path, _ = _paths(args.workspace)
    store = SnapshotStore(snap_dir)
    snapshot = store.load(args.snapshot_name)
    from .gui import build_view_model, launch
    view = build_view_model(snapshot, store)
    text = launch(view, render_output=args.render_output,
                  auto_close_ms=args.auto_close_ms)
    if text:
        print(text)
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    snap_dir, _, _, _ = _paths(args.workspace)
    store = SnapshotStore(snap_dir)
    rows = []
    for name in store.list_names():
        snap = store.load(name)
        rows.append({
            "name": snap.name, "status": snap.status,
            "params_version": snap.params_version,
            "stages_finished": snap.result_indexes(),
            "stages_unknown": snap.unknown_indexes(),
        })
    _print({"snapshots": rows})
    return 0


def _write_corrupt_scene(workspace: str) -> str:
    path = os.path.join(workspace, "corrupted-scene.json")
    os.makedirs(workspace, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write('{"name": "broken", "stages": [ { "index": 1, ,, ]')
    return path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="capbench",
                                     description="Local capacity workbench")
    parser.add_argument("--workspace", default=_default_workspace())
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run a ladder load-test session")
    run.add_argument("--scene", required=True)
    run.add_argument("--snapshot-name", required=True)
    run.add_argument("--process-ms", type=int, default=20)
    run.add_argument("--error-rate-bp", type=int, default=100)
    run.add_argument("--seed", type=int, default=20261117)
    run.add_argument("--force-stop-after-s", type=float, default=None)
    run.add_argument("--inject-corrupt-scene", action="store_true")
    run.add_argument("--inject-port-busy", action="store_true")
    run.add_argument("--inject-clock-rollback", action="store_true")
    run.add_argument("--clock-rollback-ms", type=int, default=500)
    run.add_argument("--clock-rollback-at-tick", type=int, default=8)
    run.set_defaults(func=cmd_run)

    rec = sub.add_parser("recover", help="recover state from a snapshot")
    rec.add_argument("--snapshot-name", required=True)
    rec.set_defaults(func=cmd_recover)

    rep = sub.add_parser("replay", help="consistency replay from snapshot")
    rep.add_argument("--snapshot-name", required=True)
    rep.add_argument("--inject-port-busy", action="store_true")
    rep.set_defaults(func=cmd_replay)

    lst = sub.add_parser("list", help="list snapshots")
    lst.set_defaults(func=cmd_list)

    gui = sub.add_parser("gui", help="render the Tkinter workbench UI")
    gui.add_argument("--snapshot-name", required=True)
    gui.add_argument("--render-output", default=None)
    gui.add_argument("--auto-close-ms", type=int, default=None)
    gui.set_defaults(func=cmd_gui)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
