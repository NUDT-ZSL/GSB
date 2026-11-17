"""Checkpoint crash / recovery tests (requirement 6)."""

import json

import pytest

from policy_validator import errors
from policy_validator.checkpoint import CheckpointStore
from policy_validator.models import TaskSpec
from policy_validator.parser import parse_policies
from policy_validator.scheduler import SchedulerConfig, SimulatedCrash, run_batch

from sample_gen import scenario_recovery


def _setup(tmp_path):
    rules, task_doc = scenario_recovery()
    parsed = parse_policies({"policy_version": "1.0.0", "rules": rules})
    rules_by_id = {r.rule_id: r for r in parsed.rules}
    tasks = [TaskSpec(task_id=t["task_id"], target=t["target"],
                      rule_ids=tuple(t["rule_ids"]))
             for t in task_doc["tasks"]]
    store = CheckpointStore(tmp_path, "batch-1", "fp-v1")
    return rules_by_id, tasks, store


def _save(store):
    def save(state):
        full = store.initial_state()
        full.update(state)
        store.save(full)
    return save


def test_crash_then_resume_matches_uninterrupted_run(tmp_path):
    rules_by_id, tasks, store = _setup(tmp_path)
    config = SchedulerConfig(concurrency=2)
    ref_dir = tmp_path / "ref"
    ref_dir.mkdir()
    ref_store = CheckpointStore(ref_dir, "batch-1", "fp-v1")
    reference = run_batch(tasks, rules_by_id, config,
                          checkpoint_save=_save(ref_store))

    def kill_after_three(count):
        if count >= 3:
            raise SimulatedCrash()

    with pytest.raises(SimulatedCrash):
        run_batch(tasks, rules_by_id, config,
                  checkpoint_save=_save(store), kill_hook=kill_after_three)

    state, events = store.recover()
    assert events == []
    assert state is not None
    assert len(state["completed"]) == 3

    resumed = run_batch(tasks, rules_by_id, config,
                        resumed_state=state, checkpoint_save=_save(store))

    assert [o.task_id for o in resumed.outcomes] == \
           [o.task_id for o in reference.outcomes]
    assert [o.findings for o in resumed.outcomes] == \
           [o.findings for o in reference.outcomes]
    assert all(o.status == "succeeded" for o in resumed.outcomes)


def test_policy_change_forces_full_rebuild(tmp_path):
    rules_by_id, tasks, store = _setup(tmp_path)
    config = SchedulerConfig(concurrency=2)

    def kill_after_two(count):
        if count >= 2:
            raise SimulatedCrash()

    with pytest.raises(SimulatedCrash):
        run_batch(tasks, rules_by_id, config,
                  checkpoint_save=_save(store), kill_hook=kill_after_two)

    changed = CheckpointStore(tmp_path, "batch-1", "fp-v2")
    state, events = changed.recover()
    assert state is None
    assert any(e["code"] == errors.E_POLICY_VERSION_CHANGED for e in events)


def test_structure_version_mismatch_forces_rebuild(tmp_path):
    rules_by_id, tasks, store = _setup(tmp_path)
    config = SchedulerConfig(concurrency=1)
    run_batch(tasks, rules_by_id, config, checkpoint_save=_save(store))

    data = json.loads(store.path.read_text(encoding="utf-8"))
    data["structure_version"] = 999
    store.path.write_text(json.dumps(data), encoding="utf-8")

    state, events = store.recover()
    assert state is None
    assert any(e["code"] == errors.E_CHECKPOINT_VERSION_MISMATCH
               for e in events)


def test_corrupt_checkpoint_is_not_resumed(tmp_path):
    store = CheckpointStore(tmp_path, "batch-1", "fp-v1")
    store.path.write_text('{"structure_version": 1, "pol', encoding="utf-8")
    state, events = store.recover()
    assert state is None  # torn write -> restart, never partial resume
