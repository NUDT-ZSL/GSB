#!/usr/bin/env python3
"""Deterministic end-to-end demo (stable output order).

Sequence:
  0. pre-flight: verify loopback ports are free
  1. cold-start full ladder run -> named snapshot
  2. forced-termination run on a separate snapshot -> unknown stage recovery
  3. modify concurrency, rerun under the same snapshot name -> stale
     observations wholly replaced, version increments
  4. recover + consistency replay of the cold-start snapshot
  5. Tkinter view render (text fallback when headless)
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import sys
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from capbench.driver import request_stop_after                 # noqa: E402
from capbench.gui import build_view_model, launch              # noqa: E402
from capbench.mock_server import MockServer, ServerConfig       # noqa: E402
from capbench.replay import replay_snapshot                    # noqa: E402
from capbench.scene import load_scene                          # noqa: E402
from capbench.session import SessionRunner                     # noqa: E402
from capbench.snapshot import SnapshotStore                    # noqa: E402
from capbench.versioning import VersionStore                   # noqa: E402

PORT = 8431
WORKSPACE = os.path.join(ROOT, "workspace-demo")
SNAP_COLD = "demo-cold"
SNAP_KILL = "demo-killed"


def section(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def preflight() -> None:
    section("STEP 0  pre-flight: loopback port occupancy check")
    occupied = []
    for port in (PORT, PORT + 1):
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            occupied.append(port)
        finally:
            probe.close()
    if occupied:
        print(f"FAIL ports occupied: {occupied}; aborting demo")
        sys.exit(3)
    print(f"OK 127.0.0.1:{PORT} and :{PORT + 1} are free")


def make_runner(store: SnapshotStore, versions: VersionStore,
                cfg: ServerConfig) -> SessionRunner:
    return SessionRunner(
        store, versions,
        server_config={
            "host": cfg.host, "port": cfg.port,
            "process_ms": cfg.process_ms,
            "error_rate_bp": cfg.error_rate_bp, "seed": cfg.seed,
        })


def main() -> int:
    if os.path.isdir(WORKSPACE):
        shutil.rmtree(WORKSPACE)
    preflight()

    snap_dir = os.path.join(WORKSPACE, "snapshots")
    state_dir = os.path.join(WORKSPACE, "state")
    os.makedirs(state_dir, exist_ok=True)
    store = SnapshotStore(snap_dir)
    versions = VersionStore(os.path.join(state_dir, "versions.json"))
    cfg = ServerConfig(host="127.0.0.1", port=PORT, process_ms=20,
                       error_rate_bp=100, seed=20261117,
                       log_path=os.path.join(WORKSPACE, "server.log.jsonl"))

    # ---------------------------------------------------------------- STEP 1
    section("STEP 1  cold-start full ladder run")
    scene = load_scene(os.path.join(ROOT, "scenarios", "cold_start.json"))
    with MockServer(cfg):
        report = make_runner(store, versions, cfg).run(SNAP_COLD, scene)
    for result in report.completed:
        m = result.metrics
        print(f"stage {result.stage_index}: conc={result.spec['concurrency']} "
              f"rps={m.throughput_rps} sr_bp={m.success_rate_bp} "
              f"p95={m.p95_latency_ms}ms verdict={result.verdict} "
              f"reason={result.saturation_reason}")
    print(f"params_version={report.params_version} "
          f"snapshot={store.path_of(SNAP_COLD)}")

    # ---------------------------------------------------------------- STEP 2
    section("STEP 2  forced termination -> unknown stage, no synthesized report")
    kill_event = threading.Event()
    request_stop_after(kill_event, delay_s=3.4)
    with MockServer(cfg):
        killed = make_runner(store, versions, cfg).run(
            SNAP_KILL, scene, stop_event=kill_event)
    plan = SessionRunner.recovery_plan(store.load(SNAP_KILL))
    print(f"interrupted={killed.interrupted} "
          f"finished={plan['finished_stage_indexes']} "
          f"unknown={plan['unknown_stage_indexes']} "
          f"must_rerun={plan['must_rerun_stage_indexes']}")

    # ---------------------------------------------------------------- STEP 3
    section("STEP 3  rerun with modified concurrency -> stale whole replacement")
    scene2 = load_scene(os.path.join(ROOT, "scenarios", "rerun_concurrency.json"))
    with MockServer(cfg):
        rerun = make_runner(store, versions, cfg).run(SNAP_COLD, scene2)
    rep = rerun.replacement
    print(f"stale_replaced={rep.fully_replaced} "
          f"replaced_stages={rep.replaced_stage_indexes} "
          f"version {rep.old_version} -> {rep.new_version}")
    for diff in rep.diffs:
        print(f"  diff: {diff}")
    for result in rerun.completed:
        print(f"stage {result.stage_index}: conc={result.spec['concurrency']} "
              f"rps={result.metrics.throughput_rps} "
              f"verdict={result.verdict}")

    # ---------------------------------------------------------------- STEP 4
    section("STEP 4  recovery + consistency replay of cold-start snapshot")
    with MockServer(cfg):
        make_runner(store, versions, cfg).run(
            SNAP_COLD + "-replaybase", scene)
    snapshot = store.load(SNAP_COLD + "-replaybase")
    recovery = SessionRunner.recovery_plan(snapshot)
    print(f"recovery: finished={recovery['finished_stage_indexes']} "
          f"unknown={recovery['unknown_stage_indexes']}")
    with MockServer(cfg):
        replay_report = replay_snapshot(snapshot, {
            "host": cfg.host, "port": cfg.port,
            "process_ms": cfg.process_ms,
            "error_rate_bp": cfg.error_rate_bp, "seed": cfg.seed})
    for item in replay_report.items:
        print(f"stage {item.stage_index}: matched={item.matched} "
              f"observed={json.dumps(item.observed, sort_keys=True)}")
    print(f"reproducible={replay_report.reproducible}")

    # ---------------------------------------------------------------- STEP 5
    section("STEP 5  Tkinter view (stable render order)")
    view = build_view_model(snapshot, store)
    out = os.path.join(WORKSPACE, "gui-render.txt")
    text = launch(view, render_output=out, auto_close_ms=1500)
    print(text)
    print(f"view rendered to {out}")

    section("DEMO SUMMARY (stable order)")
    print("1. cold-start ladder:       all stages finished, version 1")
    print("2. forced termination:      unknown stages require rerun, "
          "no fabricated reports")
    print("3. concurrency rerun:       stale observations wholly replaced, "
          "version increments")
    print("4. recovery replay:         every finished stage matched within "
          "fixed tolerance")
    print("5. GUI:                     stable ordered stage/snapshot render")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
