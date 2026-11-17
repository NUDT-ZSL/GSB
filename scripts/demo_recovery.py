"""End-to-end crash / recovery demonstration on local samples.

Phase 1 runs a batch that is "killed" after the third checkpoint (no
cleanup, mimicking a forced termination). Phase 2 resumes through the
CLI with --resume and verifies the merged result equals an uninterrupted
run. Usage:

    .venv/bin/python scripts/demo_recovery.py
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from policy_validator import errors  # noqa: E402
from policy_validator.checkpoint import CheckpointStore  # noqa: E402
from policy_validator.cli import _fingerprint, _load_tasks  # noqa: E402
from policy_validator.cli import run as cli_run  # noqa: E402
from policy_validator.parser import parse_policies  # noqa: E402
from policy_validator.scheduler import SchedulerConfig, SimulatedCrash  # noqa: E402
from policy_validator.scheduler import run_batch  # noqa: E402


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    scenario = root / "samples" / "recovery"
    policies = scenario / "policies.json"
    tasks_file = scenario / "tasks.json"
    workdir = root / "out" / "recovery-demo"
    refdir = root / "out" / "recovery-ref"
    for d in (workdir, refdir):
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True)

    fingerprint = _fingerprint(policies.read_bytes())
    parsed = parse_policies(json.loads(policies.read_text()))
    rules_by_id = {r.rule_id: r for r in parsed.rules}
    tasks = _load_tasks(tasks_file)
    config = SchedulerConfig(concurrency=2)

    store = CheckpointStore(workdir, "demo", fingerprint)

    def save(state):
        full = store.initial_state()
        full.update(state)
        store.save(full)

    def kill(count):
        if count >= 3:
            raise SimulatedCrash()

    try:
        run_batch(tasks, rules_by_id, config,
                  checkpoint_save=save, kill_hook=kill)
    except SimulatedCrash:
        pass
    checkpoint = json.loads((workdir / "checkpoint.json").read_text())
    print(f"phase 1: killed after {len(checkpoint['completed'])} checkpointed tasks")
    assert not (workdir / "report.json").exists(), "no report may exist after a kill"

    code = cli_run([
        "--policies", str(policies), "--tasks", str(tasks_file),
        "--workdir", str(workdir), "--resume", "--batch-id", "demo",
    ])
    report = json.loads((workdir / "report.json").read_text())
    print(f"phase 2: resumed, exit={code}, summary={report['summary']}")

    ref_code = cli_run([
        "--policies", str(policies), "--tasks", str(tasks_file),
        "--workdir", str(refdir), "--batch-id", "demo",
    ])
    reference = json.loads((refdir / "report.json").read_text())

    assert code == ref_code, (code, ref_code)
    assert report["findings"] == reference["findings"]
    assert [t["task_id"] for t in report["tasks"]] == \
           [t["task_id"] for t in reference["tasks"]]
    assert report["summary"] == reference["summary"]
    assert report["findings"]["conflicts"], "sample must contain a conflict"
    print("recovery result identical to uninterrupted run: OK")
    print(f"conflict found: {report['findings']['conflicts'][0]}")
    assert code & errors.EXIT_FINDINGS
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
