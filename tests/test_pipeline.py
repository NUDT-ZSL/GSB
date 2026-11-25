from __future__ import annotations

from health_machine.cli import main
from health_machine.status import build_status
from health_machine.store import Store
from health_machine.util import canonical_json
from health_machine.verify import verify_store


def _status_by_id(db):
    status = build_status(Store(db))
    assert status["ok"]
    return {item["instance_id"]: item for item in status["instances"]}


def test_state_machine_paths_and_verification(completed_workspace):
    states = _status_by_id(completed_workspace.db)

    assert states["inst-steady"]["state"] == "healthy"
    assert states["inst-steady"]["transition_count"] == 0

    flap = states["inst-flap"]
    assert flap["state"] == "healthy"
    assert [(t["from_state"], t["to_state"]) for t in flap["transitions"]] == [
        ("healthy", "degraded"),
        ("degraded", "lost"),
        ("lost", "healthy"),
    ]
    assert flap["transitions"][0]["trigger_level"] == 1
    assert flap["transitions"][0]["trigger_threshold"]["failure_limit"] == 2
    assert "1" in flap["transitions"][0]["evidence"]

    outage = states["inst-outage"]
    assert outage["state"] == "healthy"
    assert [(t["from_state"], t["to_state"]) for t in outage["transitions"]] == [
        ("healthy", "degraded"),
        ("degraded", "lost"),
        ("lost", "isolated"),
        ("isolated", "healthy"),
    ]
    isolated_transition = outage["transitions"][2]
    assert isolated_transition["trigger_kind"] == "joint_score_and_failure_threshold"
    assert isolated_transition["trigger_threshold"]["actual_failures"] >= 92
    assert outage["recovery_arcs"][-1]["status"] == "completed"
    assert outage["recovery_arcs"][-1]["required_windows"] == 4

    timeout = states["inst-timeout"]
    assert [(t["from_state"], t["to_state"], t["trigger_kind"]) for t in timeout["transitions"]] == [
        ("healthy", "degraded", "injected_clock_timeout"),
        ("degraded", "healthy", "joint_score_and_failure_threshold"),
    ] or timeout["transitions"][0]["trigger_kind"] == "injected_clock_timeout"

    verification = verify_store(Store(completed_workspace.db))
    assert verification["ok"], verification


def test_scores_are_deterministic_across_runs(workspace, tmp_path):
    first_db = workspace.db
    assert main(["probe", "--db", str(first_db)]) == 0
    second_db = tmp_path / "second.sqlite"
    assert main(["load", "--samples", str(workspace.samples), "--db", str(second_db)]) == 0
    assert main(["merge", "--samples", str(workspace.samples), "--db", str(second_db),
                 "--merge-version", "second"]) == 0
    assert main(["probe", "--db", str(second_db)]) == 0

    first = _status_by_id(first_db)
    second = _status_by_id(second_db)
    comparable = {}
    for iid, item in first.items():
        comparable[iid] = {
            "scores": item["latest_scores_by_level"],
            "state": item["state"],
            "hash": item["state_hash"],
            "transitions": [(t["transition_seq"], t["entry_hash"]) for t in item["transitions"]],
        }
    second_comparable = {}
    for iid, item in second.items():
        second_comparable[iid] = {
            "scores": item["latest_scores_by_level"],
            "state": item["state"],
            "hash": item["state_hash"],
            "transitions": [(t["transition_seq"], t["entry_hash"]) for t in item["transitions"]],
        }
    assert canonical_json(second_comparable) == canonical_json(comparable)


def test_cross_version_merge_provenance(workspace):
    store = Store(workspace.db)
    conn = store.read_connection()
    try:
        flap = conn.execute(
            "SELECT * FROM threshold_view WHERE instance_id='inst-flap' AND level=1"
        ).fetchone()
        assert flap["winner_format"] == "v2"
        assert flap["generation"] == 1
        decision = __import__("json").loads(flap["decision_json"])
        assert decision["reason"] == "same_clock_same_generation_format_priority"

        steady = conn.execute(
            "SELECT * FROM threshold_view WHERE instance_id='inst-steady' AND level=1"
        ).fetchone()
        assert steady["winner_format"] == "v2"
        assert steady["generation"] == 2

        v1_candidate = conn.execute(
            "SELECT time_conversion_json, failure_counts_json FROM threshold_candidates "
            "WHERE instance_id='inst-timeout' AND level=1 AND source_format='v1'"
        ).fetchone()
        conversion = __import__("json").loads(v1_candidate["time_conversion_json"])
        assert conversion["raw_value"] == "1"
        assert conversion["scale_to_ms"] == 1000
        assert conversion["rounding"] == "floor"
        assert conversion["absolute_effective_at_ms"] == 1000
        assert __import__("json").loads(v1_candidate["failure_counts_json"]) == [2, 3, 4]

        outage3 = conn.execute(
            "SELECT active, exclusion_reason, excluded_generation FROM threshold_view "
            "WHERE instance_id='inst-outage' AND level=3"
        ).fetchone()
        assert tuple(outage3) == (0, "tombstoned", 3)
    finally:
        conn.close()


def test_status_command_sorts_instances(completed_workspace, capsys):
    assert main(["status", "--db", str(completed_workspace.db)]) == 0
    output = capsys.readouterr().out
    assert output.index("inst-flap") < output.index("inst-outage") < output.index("inst-steady") < output.index("inst-timeout")
