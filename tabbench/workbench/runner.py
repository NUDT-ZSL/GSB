"""Run orchestration: load -> expand -> plan -> execute -> summarize.

Phases are snapshotted with tmp-file + atomic replace. Resume validates
snapshot version / config hash / data fingerprint and continues from the
recorded cursor; the final result of a resumed run is field-identical to
an uninterrupted run.
"""

import os
import shutil
import signal
import sys
import threading

from . import SNAPSHOT_VERSION
from .clock import elapsed_ms, ensure_monotonic, monotonic_ns
from .config import load_configs
from .data import fingerprint, generate_data
from .errors import E_CONFIG_MISMATCH, E_TASK_FAILED, TaskExecutionError
from .report import build_report
from .scheduler import build_plan
from .snapshot import (atomic_write_json, load_completed_results,
                       load_snapshot, report_path, result_path,
                       write_snapshot)
from .tasks import expand_tasks, run_task


class RunState:
    def __init__(self, config_hash, data_fingerprint, failures,
                 halt_after=None):
        self.config_hash = config_hash
        self.data_fingerprint = data_fingerprint
        self.failures = failures
        self.results = {}
        self.completed = []
        self.budget_consumed_ms = 0.0
        self.stop_event = threading.Event()
        self.halt_after = halt_after

    def progress_summary(self):
        timeouts = sum(1 for r in self.results.values() if r["timed_out"])
        return {
            "completed": len(self.completed),
            "timeout": timeouts,
            "failed": len(self.failures),
        }


def _print(out, line):
    out.write(line + "\n")
    out.flush()


def _snapshot_now(out_dir, phase, state, cursor):
    return write_snapshot(
        out_dir, phase, state.config_hash, state.data_fingerprint,
        state.completed, state.budget_consumed_ms,
        state.progress_summary(), cursor)


def _execute_plan(plan, dataset, cfg, out_dir, state):
    lock = threading.Lock()
    interval = cfg["scheduler"]["snapshot_interval"]

    def worker(queue):
        for spec in queue:
            if state.stop_event.is_set():
                return
            with lock:
                already = spec.key in state.results
            if already:
                continue
            t0 = monotonic_ns()
            try:
                res = run_task(spec, dataset, cfg)
            except TaskExecutionError as exc:
                with lock:
                    state.failures.append({
                        "phase": exc.phase,
                        "task_key": spec.key,
                        "code": E_TASK_FAILED,
                        "exc_type": type(exc.original).__name__,
                        "detail": str(exc.original)[:200],
                    })
                continue
            except Exception as exc:  # last-resort isolation
                with lock:
                    state.failures.append({
                        "phase": "train",
                        "task_key": spec.key,
                        "code": E_TASK_FAILED,
                        "exc_type": type(exc).__name__,
                        "detail": str(exc)[:200],
                    })
                continue
            t1 = monotonic_ns()
            res["observability"] = {
                "monotonic_start_ns": t0,
                "monotonic_end_ns": t1,
                "real_elapsed_ms": elapsed_ms(t0, t1),
            }
            atomic_write_json(result_path(out_dir, spec.key), res)
            with lock:
                state.results[spec.key] = res
                state.completed.append(spec.key)
                state.budget_consumed_ms += res["billed_ms"]
                done = len(state.completed)
                if done % interval == 0:
                    _snapshot_now(out_dir, "train", state,
                                  {"completed_count": done})
                if state.halt_after is not None and done >= state.halt_after:
                    state.stop_event.set()

    threads = []
    for slot_id, queue in enumerate(plan["assignments"]):
        if not queue:
            continue
        t = threading.Thread(target=worker, args=(queue,),
                             name="slot-%d" % slot_id)
        t.start()
        threads.append(t)
    for t in threads:
        t.join()


def run_workbench(config_path, data_path=None, out_dir="runs/run",
                  max_concurrency=None, fresh=False, halt_after=None,
                  out=None):
    out = out or sys.stdout
    ensure_monotonic()
    run_start_ns = monotonic_ns()

    if fresh and os.path.isdir(out_dir):
        shutil.rmtree(out_dir)
    os.makedirs(os.path.join(out_dir, "results"), exist_ok=True)

    cfg, desc, failures, config_hash = load_configs(config_path, data_path)

    if failures:
        # E_CONFIG_INVALID entries go to the failure list; the run still
        # completes and emits a report instead of aborting.
        report = build_report(
            {"scheduler": {"global_budget_ms": 0, "task_quota_ms": 0}},
            config_hash, "unavailable", [], [], None, failures,
            {"note": "config invalid; no tasks scheduled"})
        atomic_write_json(report_path(out_dir), report)
        _print(out, report["summary_line"])
        return 0

    dataset = generate_data(desc)
    data_fp = fingerprint(dataset)
    state = RunState(config_hash, data_fp, list(failures),
                     halt_after=halt_after)

    snap = load_snapshot(out_dir)
    restored = False
    if snap is not None:
        mismatch = None
        if snap.get("version") != SNAPSHOT_VERSION:
            mismatch = ("version", SNAPSHOT_VERSION, snap.get("version"))
        elif snap.get("config_hash") != config_hash:
            mismatch = ("config_hash", config_hash, snap.get("config_hash"))
        elif snap.get("data_fingerprint") != data_fp:
            mismatch = ("data_fingerprint", data_fp,
                        snap.get("data_fingerprint"))
        if mismatch is not None:
            _print(out, "%s|field=%s|expected=%s|actual=%s"
                   % (E_CONFIG_MISMATCH, mismatch[0], mismatch[1],
                      mismatch[2]))
            return 2
        restored = True
        state.completed = list(snap.get("completed", []))
        state.results = load_completed_results(out_dir, state.completed)
        state.completed = [k for k in state.completed if k in state.results]
        state.budget_consumed_ms = sum(
            state.results[k]["billed_ms"] for k in state.completed)
        _print(out, "RESTORE|phase=%s|completed=%d|budget_consumed_ms=%.3f"
               % (snap.get("phase"), len(state.completed),
                  snap.get("budget_consumed_ms", 0.0)))

    # Phase 1: grid expansion.
    specs = expand_tasks(cfg)
    _snapshot_now(out_dir, "expand", state, {"task_count": len(specs)})

    # Phase 2: deterministic plan + concurrent execution.
    concurrency = max_concurrency or cfg["scheduler"]["max_concurrency"]
    plan = build_plan(specs, dataset, cfg, concurrency)

    old_handlers = {}

    def _handle_signal(signum, frame):
        state.stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        old_handlers[sig] = signal.signal(sig, _handle_signal)
    try:
        _execute_plan(plan, dataset, cfg, out_dir, state)
    finally:
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)

    if state.stop_event.is_set():
        _snapshot_now(out_dir, "train", state,
                      {"completed_count": len(state.completed)})
        _print(out, "INTERRUPTED|completed=%d|snapshot=%s"
               % (len(state.completed),
                  os.path.join(out_dir, "snapshot.json")))
        return 0

    # Phase 3: summarize.
    _snapshot_now(out_dir, "summarize", state,
                  {"completed_count": len(state.completed)})
    run_end_ns = monotonic_ns()
    observability = {
        "wall_elapsed_ms": elapsed_ms(run_start_ns, run_end_ns),
        "restored_from_snapshot": restored,
        "max_concurrency": concurrency,
        "clock": "monotonic",
    }
    report = build_report(cfg, config_hash, data_fp, specs, state.results,
                          plan, state.failures, observability)
    atomic_write_json(report_path(out_dir), report)
    _snapshot_now(out_dir, "done", state,
                  {"completed_count": len(state.completed)})
    _print(out, report["summary_line"])
    return 0
