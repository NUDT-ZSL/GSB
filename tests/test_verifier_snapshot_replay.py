import json
import os

import pytest

from conftest import FAIL_FIRST_TEMPLATE, write_script
from orchestrator.dag_loader import dag_fingerprint, parse_dag
from orchestrator.errors import ReplayError, VerifyError
from orchestrator.replay import replay_snapshot
from orchestrator.scheduler import Scheduler
from orchestrator.snapshot import (
    VersionDecision,
    compare_version,
    load_snapshot,
    recover_completed,
)
from orchestrator.verifier import verify_or_raise, verify_snapshot


def _schedule(simple_env, tmp_workspace, failure_config=None):
    workspace, dag_obj = simple_env
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


def test_verifier_passes_for_clean_run(simple_env, tmp_workspace):
    _, snapshot, _ = _schedule(simple_env, tmp_workspace)
    report = verify_or_raise(snapshot, str(tmp_workspace["work"]))
    assert report.ok


def test_verifier_detects_tampered_output(simple_env, tmp_workspace):
    _, snapshot, _ = _schedule(simple_env, tmp_workspace)
    target = os.path.join(tmp_workspace["work"], "outputs", "a.jsonl")
    with open(target, "a", encoding="utf-8", newline="\n") as handle:
        handle.write('{"x": 1}\n')
    with pytest.raises(VerifyError) as exc:
        verify_or_raise(snapshot, str(tmp_workspace["work"]))
    details = exc.value.details[0]
    assert details["code"] == "E_VERIFY"
    assert details["deviations"][0]["stage_id"] == "a"


def test_verifier_flags_budget_violation_in_trace(simple_env, tmp_workspace):
    _, snapshot, _ = _schedule(simple_env, tmp_workspace)
    # Corrupt the trace by inflating an acquisition.
    for event in snapshot.trace:
        if event.event == "state_change" and event.reason == "ready_to_running":
            event.resource_units = 99
            break
    report = verify_snapshot(snapshot, str(tmp_workspace["work"]))
    assert not report.ok
    assert any(
        deviation["check"] == "resource_budget"
        for deviation in report.deviations
    )


def test_snapshot_version_match_and_replace(simple_env, tmp_workspace):
    _, dag_obj = simple_env
    _, snapshot, _ = _schedule(simple_env, tmp_workspace)
    fingerprint = dag_fingerprint(dag_obj, str(tmp_workspace["dag_dir"]))
    match = compare_version(snapshot, dag_obj.dag_version, fingerprint)
    assert match.action == VersionDecision.MATCH
    replace = compare_version(snapshot, "2.0.0", fingerprint)
    assert replace.action == VersionDecision.REPLACE
    assert any("dag_version differs" in reason for reason in replace.reasons)


def test_hard_stop_only_restores_completed(simple_env, tmp_workspace):
    scheduler, snapshot, snapshot_path = _schedule(simple_env, tmp_workspace)
    reopened = load_snapshot(snapshot_path)
    recovered = recover_completed(reopened)
    assert sorted(recovered) == ["a", "b", "c"]
    unknown = scheduler.mark_unknown_after_crash(reopened)
    assert unknown == []

    # Simulate an interrupted run by pruning the completed set to one stage.
    reopened.completed_stages = ["a"]
    pruned = recover_completed(reopened)
    assert sorted(pruned) == ["a"]
    unknown = scheduler.mark_unknown_after_crash(reopened)
    assert unknown == ["b", "c"]
    assert all(
        result.state in ("succeeded", "unknown")
        for result in scheduler.results.values()
    )


def test_replay_reproduces_all_digests(simple_env, tmp_workspace):
    _, snapshot, _ = _schedule(simple_env, tmp_workspace)
    replay_root = os.path.join(tmp_workspace["root"], "replay")
    report = replay_snapshot(
        snapshot=snapshot,
        base_dir=str(tmp_workspace["root"]),
        replay_root=str(replay_root),
    )
    assert report.ok
    assert all(item.ok for item in report.items)


def test_replay_reports_script_drift(simple_env, tmp_workspace):
    _, snapshot, _ = _schedule(simple_env, tmp_workspace)
    write_script(
        tmp_workspace["stages_dir"],
        "stage_a.py",
        SIMPLE_DRIFT_SCRIPT,
    )
    replay_root = os.path.join(tmp_workspace["root"], "replay-drift")
    with pytest.raises(ReplayError) as exc:
        replay_snapshot(
            snapshot=snapshot,
            base_dir=str(tmp_workspace["root"]),
            replay_root=str(replay_root),
        )
    payload = exc.value.details[0]
    assert payload["code"] == "E_REPLAY"
    assert any(
        "fingerprint_mismatch" in item["reason"] for item in payload["items"]
    )


def test_replay_reports_digest_mismatch_when_output_changes(
    simple_env, tmp_workspace
):
    _, snapshot, _ = _schedule(simple_env, tmp_workspace)
    # Keep fingerprint stable but make stage a produce different bytes at replay.
    write_script(
        tmp_workspace["stages_dir"],
        "stage_a.py",
        FAIL_FIRST_TEMPLATE,
    )
    snapshot.dag_fingerprint = dag_fingerprint(
        parse_dag(snapshot.dag_definition), str(tmp_workspace["dag_dir"])
    )
    replay_root = os.path.join(tmp_workspace["root"], "replay-mismatch")
    with pytest.raises(ReplayError) as exc:
        replay_snapshot(
            snapshot=snapshot,
            base_dir=str(tmp_workspace["root"]),
            replay_root=str(replay_root),
        )
    payload = exc.value.details[0]
    assert payload["code"] == "E_REPLAY"
    failed = [item for item in payload["items"] if not item["ok"]]
    assert failed and failed[0]["stage_id"] == "a"


SIMPLE_DRIFT_SCRIPT = (
    "import json, os\n"
    "out = os.environ['PIPE_OUTPUT_PATH']\n"
    "os.makedirs(os.path.dirname(out), exist_ok=True)\n"
    "with open(out, 'w', encoding='utf-8', newline='\\n') as fh:\n"
    "    fh.write(json.dumps({'stage': os.environ['PIPE_STAGE_ID'], 'drift': 1}, sort_keys=True) + '\\n')\n"
)


def test_snapshot_round_trip_is_stable(simple_env, tmp_workspace):
    _, snapshot, snapshot_path = _schedule(simple_env, tmp_workspace)
    first = open(snapshot_path, "r", encoding="utf-8").read()
    reopened = load_snapshot(snapshot_path)
    from orchestrator.snapshot import save_snapshot

    save_snapshot(snapshot_path + ".copy", reopened)
    second = open(snapshot_path + ".copy", "r", encoding="utf-8").read()
    assert json.loads(first) == json.loads(second)
