from __future__ import annotations

import copy

from tactical_engine.replay import replay_scenario


def test_snapshot_resume_matches_full_replay(sample_scenario):
    full = replay_scenario(sample_scenario)
    snapshot = copy.deepcopy(full.snapshots[2])
    resumed = replay_scenario(sample_scenario, snapshot)
    assert resumed.resume.accepted
    assert resumed.decisions == full.decisions
    assert resumed.final_units == full.final_units
    assert resumed.digest == full.digest
    assert resumed.snapshots[-1] == full.snapshots[-1]


def test_snapshot_version_mismatch_restarts(sample_scenario):
    full = replay_scenario(sample_scenario)
    snapshot = copy.deepcopy(full.snapshots[2])
    snapshot["version"] = "f" * 64
    resumed = replay_scenario(sample_scenario, snapshot)
    assert resumed.resume.requested
    assert not resumed.resume.accepted
    assert resumed.resume.reason == "E_SNAPSHOT_VERSION_MISMATCH"
    assert resumed.decisions == full.decisions
    assert resumed.final_units == full.final_units
    assert resumed.digest == full.digest


def test_tampered_snapshot_state_restarts(sample_scenario):
    full = replay_scenario(sample_scenario)
    snapshot = copy.deepcopy(full.snapshots[2])
    snapshot["units"]["alpha"]["remaining_budget_fixed"] += 1
    resumed = replay_scenario(sample_scenario, snapshot)
    assert not resumed.resume.accepted
    assert resumed.resume.reason == "E_SNAPSHOT_STATE_MISMATCH"
    assert resumed.decisions == full.decisions
    assert resumed.digest == full.digest


def test_tampered_snapshot_decisions_restart(sample_scenario):
    full = replay_scenario(sample_scenario)
    snapshot = copy.deepcopy(full.snapshots[2])
    snapshot["decisions"][0]["decision"] = "tampered"
    resumed = replay_scenario(sample_scenario, snapshot)
    assert not resumed.resume.accepted
    assert resumed.resume.reason == "E_SNAPSHOT_DECISIONS"
    assert resumed.decisions == full.decisions
    assert resumed.digest == full.digest
