"""Repeatable end-to-end demonstration over the fixed local samples.

The script never reads the wall clock: every step takes its time from
``samples/clock_steps.json``.  Run with the project venv:

    python demo.py

All artifacts (SQLite DB, WAL files, logs, captured JSON output) are written
under ``run/demo`` inside the workspace.
"""

import json
import os
import shutil
import sys

from dqueue.backoff import backoff_delay_ms
from dqueue.cli import run as cli_run
from dqueue.config import QueueConfig
from dqueue.errors import DQError
from dqueue.manifest import load_files
from dqueue.queue import DelayedQueue
from dqueue.scheduler import HeapScheduler, WheelScheduler

ROOT = os.path.dirname(os.path.abspath(__file__))
SAMPLES = os.path.join(ROOT, "samples")
RUN = os.path.join(ROOT, "run", "demo")
LOG = os.path.join(RUN, "logs")


def ensure_clean_dir(path):
    os.makedirs(path, exist_ok=True)
    for name in os.listdir(path):
        target = os.path.join(path, name)
        if os.path.isdir(target):
            shutil.rmtree(target)
        else:
            os.remove(target)


class Recorder:
    def __init__(self):
        self.lines = []

    def section(self, title):
        line = f"\n===== {title} ====="
        self.lines.append(line)
        print(line)

    def record(self, title, value):
        self.lines.append(f"--- {title} ---")
        text = json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False)
        self.lines.append(text)
        print(text)

    def flush(self, path):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(self.lines) + "\n")


def section_scheduler_equivalence(rec, tasks, steps):
    rec.section("1. heap vs timing-wheel expiration equivalence")
    cfg = QueueConfig.from_dict(
        json.load(open(os.path.join(SAMPLES, "queue_config.json"))))
    entries = [(t["fire_ms"], t["task_id"]) for t in tasks]
    heap = HeapScheduler()
    wheel = WheelScheduler(cfg.wheel, 0)
    for fire_ms, task_id in entries:
        heap.add(fire_ms, task_id, 1)
        wheel.add(fire_ms, task_id, 1)
    heap_seq, wheel_seq = [], []
    for step in steps:
        heap_seq.extend(tid for _, tid, _ in heap.advance(step))
        wheel_seq.extend(tid for _, tid, _ in wheel.advance(step))
    rec.record("heap sequence", heap_seq)
    rec.record("wheel sequence", wheel_seq)
    rec.record("byte-identical", heap_seq == wheel_seq)


def main():
    ensure_clean_dir(RUN)
    os.makedirs(LOG, exist_ok=True)
    rec = Recorder()
    with open(os.path.join(SAMPLES, "clock_steps.json")) as fh:
        steps = json.load(fh)["steps_ms"]
    tasks, policies = load_files(
        os.path.join(SAMPLES, "manifest.json"),
        os.path.join(SAMPLES, "policies.json"))

    section_scheduler_equivalence(rec, tasks, steps)

    db = os.path.join(RUN, "demo.db")
    cfg_path = os.path.join(SAMPLES, "queue_config.json")
    argv_base = ["--db", db, "--config", cfg_path]

    rec.section("2. enqueue validated manifest")
    rec.record("enqueue", _invoke(argv_base + [
        "enqueue", "--manifest", os.path.join(SAMPLES, "manifest.json"),
        "--policies", os.path.join(SAMPLES, "policies.json"),
        "--now", "0"]))

    rec.section("3. adaptive scans and deterministic failure handling")
    # Drive the full clock ladder; fail every poll of t-002 until it dead
    # letters, succeed everything else.
    failure_plan = {"t-002"}
    for step in steps[1:]:
        scan = _invoke(argv_base + ["scan", "--now", str(step)])
        if scan.get("triggered"):
            rec.record(f"scan@{step}", scan)
        topics = {t["topic"] for t in tasks}
        for topic in sorted(topics):
            polled = _invoke(argv_base + [
                "poll", "--topic", topic, "--consumer", "demo-consumer",
                "--now", str(step)])
            for item in polled.get("tasks", []):
                if item["task_id"] in failure_plan:
                    ack = _invoke(argv_base + [
                        "ack", "--pull-id", item["pull_id"],
                        "--consumer", "demo-consumer",
                        "--now", str(step + 10), "--failure",
                        "--error-code", "E_DEMO_FAILURE"])
                    rec.record(f"ack FAIL {item['task_id']}@{step+10}", ack)
                else:
                    ack = _invoke(argv_base + [
                        "ack", "--pull-id", item["pull_id"],
                        "--consumer", "demo-consumer",
                        "--now", str(step + 10), "--success"])
                    rec.record(f"ack OK {item['task_id']}@{step+10}", ack)

    rec.section("4. backoff reproducibility (jittered policy)")
    policy = policies["jittered"]
    samples = [backoff_delay_ms(policy, attempt, "t-002")
               for attempt in range(1, 5)]
    again = [backoff_delay_ms(policy, attempt, "t-002")
             for attempt in range(1, 5)]
    rec.record("delays", samples)
    rec.record("recomputed identical", samples == again)

    rec.section("5. error-code matrix")
    errors = []
    for call in (
        ["ack", "--pull-id", "missing-pull", "--consumer", "c",
         "--now", "20000", "--success"],
        ["scan", "--now", "1"],
    ):
        errors.append(_invoke(argv_base + call, expect_error=True))
    q = DelayedQueue(db, QueueConfig.from_dict(
        json.load(open(cfg_path))))
    try:
        q._task_row("does-not-exist")
    except DQError as exc:
        errors.append(exc.to_dict())
    try:
        q.set_mode("heap", 20001, version=1)
    except DQError as exc:
        errors.append(exc.to_dict())
    q.close()
    rec.record("distinct business errors", errors)

    rec.section("6. final status and verification")
    status = _invoke(argv_base + ["status"])
    rec.record("status (stable sorted JSON)", status)
    rec.record("verify", _invoke(argv_base + ["verify"]))

    rec.flush(os.path.join(LOG, "demo-run.json"))
    print(f"\nartifacts: {RUN}")
    print(f"log: {os.path.join(LOG, 'demo-run.json')}")


def _invoke(argv, expect_error=False):
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cli_run(argv)
    payload = json.loads(buf.getvalue())
    if expect_error:
        assert code != 0, payload
    return payload


if __name__ == "__main__":
    sys.exit(main())
