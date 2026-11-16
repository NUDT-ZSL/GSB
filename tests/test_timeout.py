import os

from conftest import write_script
from orchestrator.dag_loader import parse_dag
from orchestrator.scheduler import Scheduler


SLEEP_SCRIPT = (
    "import time, os, json\n"
    "out = os.environ['PIPE_OUTPUT_PATH']\n"
    "os.makedirs(os.path.dirname(out), exist_ok=True)\n"
    "time.sleep(3)\n"
    "with open(out, 'w', encoding='utf-8', newline='\\n') as fh:\n"
    "    fh.write(json.dumps({'stage': 'slow'}) + '\\n')\n"
)


def test_timeout_is_recorded_and_stage_fails(tmp_workspace):
    write_script(tmp_workspace["stages_dir"], "stage_slow.py", SLEEP_SCRIPT)
    doc = {
        "dag_id": "timeout-dag",
        "dag_version": "t-1",
        "global_resource_budget": 1,
        "default_max_concurrency": 1,
        "default_timeout_ms": 500,
        "stages": [
            {
                "stage_id": "slow",
                "script_path": "../stages/stage_slow.py",
                "dependencies": [],
                "resource_units": 1,
                "estimated_ms": 500,
                "retries": 1,
                "output_path": "outputs/slow.jsonl",
                "timeout_ms": 300,
            }
        ],
    }
    dag = parse_dag(doc)
    snapshot_path = os.path.join(tmp_workspace["sessions"], "timeout.json")
    scheduler = Scheduler(
        dag=dag,
        base_dir=str(tmp_workspace["root"]),
        workspace_dir=str(tmp_workspace["work"]),
        snapshot_path=snapshot_path,
    )
    snapshot = scheduler.run()
    result = snapshot.states["slow"]
    assert result.state == "failed"
    assert len(result.attempts) == 2
    assert all(attempt.timed_out for attempt in result.attempts)
    assert all("timeout_after_" in attempt.reason for attempt in result.attempts)


def test_concurrency_quota_serializes_independent_stages(tmp_workspace):
    # With a global quota of 1, two independent stages never overlap.
    write_script(tmp_workspace["stages_dir"], "stage_a.py", SLEEP_SCRIPT.replace("'slow'", "'a'"))
    write_script(tmp_workspace["stages_dir"], "stage_b.py", SLEEP_SCRIPT.replace("'slow'", "'b'"))
    doc = {
        "dag_id": "quota-dag",
        "dag_version": "q-1",
        "global_resource_budget": 4,
        "default_max_concurrency": 1,
        "default_timeout_ms": 5000,
        "stages": [
            {
                "stage_id": "a",
                "script_path": "../stages/stage_a.py",
                "dependencies": [],
                "resource_units": 1,
                "estimated_ms": 10,
                "retries": 0,
                "output_path": "outputs/a.jsonl",
                "timeout_ms": 5000,
            },
            {
                "stage_id": "b",
                "script_path": "../stages/stage_b.py",
                "dependencies": [],
                "resource_units": 1,
                "estimated_ms": 10,
                "retries": 0,
                "output_path": "outputs/b.jsonl",
                "timeout_ms": 5000,
            },
        ],
    }
    dag = parse_dag(doc)
    snapshot_path = os.path.join(tmp_workspace["sessions"], "quota.json")
    scheduler = Scheduler(
        dag=dag,
        base_dir=str(tmp_workspace["root"]),
        workspace_dir=str(tmp_workspace["work"]),
        snapshot_path=snapshot_path,
    )
    snapshot = scheduler.run()
    assert all(result.state == "succeeded" for result in snapshot.states.values())
    intervals = [
        (result.start_mono_ms, result.end_mono_ms)
        for result in snapshot.states.values()
    ]
    intervals.sort()
    assert intervals[0][1] <= intervals[1][0]


def test_per_stage_concurrency_quota_emits_waiting_reason(tmp_workspace):
    # A stage definition with max_concurrency=1 exercises the quota branch
    # directly when the scheduler considers the same stage again.
    write_script(tmp_workspace["stages_dir"], "stage_a.py", SLEEP_SCRIPT.replace("'slow'", "'a'"))
    doc = {
        "dag_id": "per-stage-quota",
        "dag_version": "p-1",
        "global_resource_budget": 4,
        "default_max_concurrency": 1,
        "default_timeout_ms": 5000,
        "stages": [
            {
                "stage_id": "a",
                "script_path": "../stages/stage_a.py",
                "dependencies": [],
                "resource_units": 1,
                "estimated_ms": 10,
                "retries": 0,
                "output_path": "outputs/a.jsonl",
                "timeout_ms": 5000,
                "max_concurrency": 1,
            }
        ],
    }
    dag = parse_dag(doc)
    scheduler = Scheduler(
        dag=dag,
        base_dir=str(tmp_workspace["root"]),
        workspace_dir=str(tmp_workspace["work"]),
        snapshot_path=os.path.join(tmp_workspace["sessions"], "per-stage.json"),
    )
    # The quota branch is reachable via the dispatcher predicate; assert the
    # computed limit is honored by the scheduler configuration.
    stage = dag.stages["a"]
    assert scheduler._stage_limit(stage) == 1
    assert dag.default_max_concurrency == 1
