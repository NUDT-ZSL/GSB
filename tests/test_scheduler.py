import os

from conftest import FAIL_FIRST_TEMPLATE, write_script
from orchestrator.scheduler import Scheduler
from orchestrator.snapshot import load_snapshot
from orchestrator.verifier import verify_snapshot


def _run(simple_env, tmp_workspace, failure_config=None, dag=None):
    workspace, dag_obj = simple_env
    dag_obj = dag or dag_obj
    snapshot_path = os.path.join(tmp_workspace["sessions"], "s.json")
    scheduler = Scheduler(
        dag=dag_obj,
        base_dir=str(tmp_workspace["root"]),
        workspace_dir=str(tmp_workspace["work"]),
        snapshot_path=snapshot_path,
        failure_config=failure_config or {},
    )
    snapshot = scheduler.run()
    return scheduler, snapshot, snapshot_path


def test_full_success_and_snapshot_persistence(simple_env, tmp_workspace):
    _, snapshot, snapshot_path = _run(simple_env, tmp_workspace)
    on_disk = load_snapshot(snapshot_path)
    assert on_disk.session_id == snapshot.session_id
    assert {sid: result.state for sid, result in snapshot.states.items()} == {
        "a": "succeeded",
        "b": "succeeded",
        "c": "succeeded",
    }
    for result in snapshot.states.values():
        assert result.output_sha256 and len(result.output_sha256) == 64


def test_strong_failure_skips_downstream(simple_env, tmp_workspace):
    workspace, _ = simple_env
    write_script(workspace["stages_dir"], "stage_a.py", FAIL_FIRST_TEMPLATE)
    _, snapshot, _ = _run(simple_env, tmp_workspace, {"a": 1})
    states = {sid: result.state for sid, result in snapshot.states.items()}
    assert states == {"a": "failed", "b": "skipped", "c": "skipped"}
    assert snapshot.states["b"].skipped_by == "a"
    assert snapshot.states["c"].skipped_by == "a"


def test_retry_records_every_attempt_then_succeeds(simple_env, tmp_workspace):
    workspace, dag_obj = simple_env
    write_script(workspace["stages_dir"], "stage_a.py", FAIL_FIRST_TEMPLATE)
    doc = dag_obj.to_dict()
    for stage in doc["stages"]:
        if stage["stage_id"] == "a":
            stage["retries"] = 2
    from orchestrator.dag_loader import parse_dag

    dag_obj = parse_dag(doc)
    _, snapshot, _ = _run(
        simple_env, tmp_workspace, {"a": 2}, dag=dag_obj
    )
    result = snapshot.states["a"]
    assert result.state == "succeeded"
    assert [attempt.attempt_no for attempt in result.attempts] == [1, 2, 3]
    assert [attempt.exit_code for attempt in result.attempts[:2]] == [7, 7]


def test_weak_failure_does_not_skip_but_is_annotated(simple_env, tmp_workspace):
    workspace, dag_obj = simple_env
    write_script(workspace["stages_dir"], "stage_b.py", FAIL_FIRST_TEMPLATE)
    _, snapshot, _ = _run(simple_env, tmp_workspace, {"b": 5})
    states = {sid: result.state for sid, result in snapshot.states.items()}
    assert states["b"] == "failed"
    assert states["c"] == "succeeded"
    assert snapshot.states["c"].missing_upstreams == ["b"]


def test_resource_budget_never_exceeded_and_waiting_recorded(
    simple_env, tmp_workspace
):
    from orchestrator.dag_loader import parse_dag

    # Two independent 2-unit roots with a budget of 2 force one to wait.
    tight_doc = {
        "dag_id": "tight",
        "dag_version": "t-1",
        "global_resource_budget": 2,
        "default_max_concurrency": 2,
        "default_timeout_ms": 10000,
        "stages": [
            {
                "stage_id": "a",
                "script_path": "../stages/stage_a.py",
                "dependencies": [],
                "resource_units": 2,
                "estimated_ms": 10,
                "retries": 0,
                "output_path": "outputs/a.jsonl",
                "timeout_ms": 10000,
            },
            {
                "stage_id": "b",
                "script_path": "../stages/stage_b.py",
                "dependencies": [],
                "resource_units": 2,
                "estimated_ms": 10,
                "retries": 0,
                "output_path": "outputs/b.jsonl",
                "timeout_ms": 10000,
            },
        ],
    }
    tight_dag = parse_dag(tight_doc)
    _, snapshot, snapshot_path = _run(
        simple_env, tmp_workspace, dag=tight_dag
    )
    report = verify_snapshot(snapshot, str(tmp_workspace["work"]))
    assert report.ok
    assert report.checks["resource_budget"]["peak_observed"] <= 2
    waiting = [event for event in snapshot.trace if event.event == "waiting"]
    assert waiting
    assert any(
        event.reason == "resource_budget_unavailable" for event in waiting
    )


def test_no_duplicate_success_records(simple_env, tmp_workspace):
    _, snapshot, _ = _run(simple_env, tmp_workspace)
    success_events = [
        event
        for event in snapshot.trace
        if event.event == "state_change"
        and event.reason == "running_to_succeeded"
    ]
    stage_ids = [event.stage_id for event in success_events]
    assert len(stage_ids) == len(set(stage_ids))
