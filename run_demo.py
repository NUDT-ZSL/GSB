"""Repeatable end-to-end demonstration and acceptance matrix."""

import json
import os
import shutil
import signal
import subprocess
import sys
import time

from prefix_cache_workbench.snapshot import SnapshotStore
from prefix_cache_workbench.pipeline import (
    clean_runtime_dirs,
    load_json,
    load_jsonl,
    run_stream,
)
from prefix_cache_workbench.report import read_report
from prefix_cache_workbench.samplegen import generate_sample_set


ROOT = os.path.dirname(os.path.abspath(__file__))
SAMPLE_DIR = os.path.join(ROOT, "samples")
RUNTIME = os.path.join(ROOT, "runtime")


def paths(name):
    snapshot_dir = os.path.join(RUNTIME, f"snapshots_{name}")
    report_path = os.path.join(RUNTIME, "reports", f"{name}.json")
    return snapshot_dir, report_path


def run_case(name, **kwargs):
    snapshot_dir, report_path = paths(name)
    defaults = dict(
        sample_dir=SAMPLE_DIR,
        report_path=report_path,
        snapshot_dir=snapshot_dir,
        run_id=name,
        clean=True,
    )
    defaults.update(kwargs)
    _, report = run_stream(**defaults)
    return report


def final_block_map(report):
    return {
        session["session"]: session["chain_fps"]
        for session in report["sessions"]
    }


def compare(a, b, keys):
    diffs = []
    for key in keys:
        if a.get(key) != b.get(key):
            diffs.append(key)
    return diffs


def main():
    if os.path.exists(RUNTIME):
        shutil.rmtree(RUNTIME)
    generate_sample_set(SAMPLE_DIR)
    os.makedirs(os.path.join(RUNTIME, "reports"), exist_ok=True)

    requests = load_jsonl(os.path.join(SAMPLE_DIR, "request_stream.jsonl"))
    summary = {"cases": [], "checks": []}

    baseline = run_case(
        "baseline_empty_cache",
        mode="serial",
        config_override={
            "residency_budget_blocks": 10_000,
            "session_retention_cap": 10_000,
        },
    )
    serial = run_case("serial_default", mode="serial")
    concurrent = run_case("concurrent_default", mode="concurrent")
    lfu = run_case(
        "lfu_tight_budget",
        mode="concurrent",
        config_override={"eviction_policy": "LFU",
                         "residency_budget_blocks": 6},
    )
    lru = run_case(
        "lru_tight_budget",
        mode="concurrent",
        config_override={"residency_budget_blocks": 6},
    )
    order = list(range(len(requests)))
    shuffled = order[1::2] + order[0::2]
    shuffled_report = run_case(
        "shuffled_arrival",
        mode="concurrent",
        request_order=shuffled,
    )
    repeat = run_case("concurrent_repeat", mode="concurrent")

    cases = [
        baseline, serial, concurrent, lfu, lru, shuffled_report, repeat
    ]
    for report in cases:
        summary["cases"].append(
            {
                "run_id": report["run_id"],
                "event_counts": report["event_counts"],
                "failures": len(report["failures"]),
                "peak_resident": report["residency"]["peak_resident_blocks"],
                "injection_hits": report["injection_hits"],
                "digest": report["chain_fingerprint_digest"],
            }
        )

    base_blocks = final_block_map(baseline)
    for report in (serial, concurrent, lfu, lru, shuffled_report):
        same = final_block_map(report) == base_blocks
        summary["checks"].append(
            {
                "name": f"final_blocks_match_baseline:{report['run_id']}",
                "passed": same,
            }
        )

    byte_identical = compare(
        concurrent,
        repeat,
        [
            "events",
            "failures",
            "sessions",
            "residency",
            "chain_fingerprint_digest",
            "injection_hits",
        ],
    )
    summary["checks"].append(
        {"name": "repeat_run_field_identical", "passed": not byte_identical,
         "diff": byte_identical}
    )
    summary["checks"].append(
        {
            "name": "serial_concurrent_identical",
            "passed": compare(
                serial,
                concurrent,
                ["events", "failures", "sessions", "residency"],
            )
            == [],
        }
    )

    expected_codes = {
        "E_EMPTY_CONTEXT",
        "E_TOKEN_UNKNOWN",
        "E_SESSION_LIMIT_EXCEEDED",
        "E_CHAIN_BROKEN",
    }
    observed = {f["code"] for f in concurrent["failures"]}
    observed |= {
        event["type"]
        for event in concurrent["events"]
        if event["type"]
        in ("E_CHAIN_BROKEN", "E_SESSION_LIMIT_EXCEEDED")
    }
    observed |= {
        event["type"]
        for event in lru["events"]
        if event["type"] == "E_CHAIN_BROKEN"
    }
    observed |= set(concurrent["injection_hits"])
    summary["checks"].append(
        {
            "name": "injection_codes_observed",
            "passed": expected_codes.issubset(observed),
            "observed": sorted(observed),
        }
    )

    import threading

    snapshot_dir, report_path = paths("snapshot_recovery")
    SnapshotStore.clean(snapshot_dir)
    partial_requests = requests[:5]
    _, partial = run_stream(
        sample_dir=SAMPLE_DIR,
        report_path=os.path.join(
            RUNTIME, "reports", "snapshot_recovery_partial.json"
        ),
        snapshot_dir=snapshot_dir,
        run_id="snapshot_recovery_partial",
        mode="serial",
        clean=False,
    )
    partial_engine_cursor = partial["request_cursor"]
    stop_event = threading.Event()
    stop_event.set()
    _, recovered = run_stream(
        sample_dir=SAMPLE_DIR,
        report_path=report_path,
        snapshot_dir=snapshot_dir,
        run_id="snapshot_recovery",
        mode="serial",
        clean=False,
        stop_event=stop_event,
    )
    summary["checks"].append(
        {
            "name": "snapshot_resume_keeps_cursor",
            "passed": recovered["snapshot"]["recovery"]["recovered"] is True
            and recovered["request_cursor"] == partial_engine_cursor
            and recovered["stop_reason"] == "stop_signal",
        }
    )

    signal_dir = os.path.join(RUNTIME, "snapshots_signal")
    signal_report = os.path.join(RUNTIME, "reports", "signal_exit.json")
    SnapshotStore.clean(signal_dir)
    env = dict(os.environ)
    env["PCW_SNAPSHOT_DIR"] = signal_dir
    env["PCW_REPORT_PATH"] = signal_report
    proc = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "signal_runner.py")],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    time.sleep(0.4)
    proc.send_signal(signal.SIGTERM)
    stdout, stderr = proc.communicate(timeout=10)
    signal_report_data = read_report(signal_report)
    summary["checks"].append(
        {
            "name": "graceful_signal_exit_code_zero",
            "passed": proc.returncode == 0
            and signal_report_data["stop_reason"] == "stop_signal",
            "returncode": proc.returncode,
            "stderr": stderr.decode("utf-8")[:200],
        }
    )

    stale_dir = os.path.join(RUNTIME, "snapshots_stale")
    stale_report = os.path.join(RUNTIME, "reports", "stale.json")
    SnapshotStore.clean(stale_dir)
    run_stream(
        sample_dir=SAMPLE_DIR,
        report_path=os.path.join(RUNTIME, "reports", "stale_seed.json"),
        snapshot_dir=stale_dir,
        run_id="stale_seed",
        mode="serial",
        clean=False,
    )
    _, stale = run_stream(
        sample_dir=SAMPLE_DIR,
        report_path=stale_report,
        snapshot_dir=stale_dir,
        run_id="stale_config",
        mode="serial",
        clean=False,
        config_override={"residency_budget_blocks": 7},
    )
    summary["checks"].append(
        {
            "name": "config_hash_mismatch_marks_stale",
            "passed": stale["recovery_decision"]["code"]
            == "E_SNAPSHOT_STALE",
        }
    )

    summary["all_passed"] = all(item["passed"] for item in summary["checks"])
    with open(os.path.join(RUNTIME, "reports", "demo_summary.json"), "w",
              encoding="utf-8") as handle:
        json.dump(summary, handle, sort_keys=True, ensure_ascii=False, indent=2)
    print(json.dumps(summary, sort_keys=True, indent=2))
    return 0 if summary["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
