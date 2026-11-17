"""Concurrency equivalence, queue limits and scheduling summary tests."""

from policy_validator import errors
from policy_validator.models import TaskSpec
from policy_validator.parser import parse_policies
from policy_validator.report import build_report, build_summary
from policy_validator.scheduler import SchedulerConfig, plan_tasks, run_batch

from sample_gen import scenario_concurrency, scenario_queue_full


def _rules_by_id(rules):
    parsed = parse_policies({"policy_version": "1.0.0", "rules": rules})
    return {r.rule_id: r for r in parsed.rules}


def _run(rules, tasks, **overrides):
    config = SchedulerConfig(**overrides)
    return run_batch(tasks, _rules_by_id(rules), config)


def test_conclusions_identical_across_concurrency_levels():
    rules, task_doc = scenario_concurrency()
    tasks = [TaskSpec(task_id=t["task_id"], target=t["target"],
                      rule_ids=tuple(t["rule_ids"]))
             for t in task_doc["tasks"]]
    results = {}
    for concurrency in (1, 2, 4, 8):
        batch = _run(rules, tasks, concurrency=concurrency)
        findings = [o.findings for o in batch.outcomes if o.findings]
        conflicts = sorted(tuple(c["rules"]) for f in findings
                           for c in f["conflicts"])
        shadows = sorted(tuple(s["rules"]) for f in findings
                         for s in f["shadows"])
        results[concurrency] = (conflicts, shadows,
                                [o.status for o in batch.outcomes])
    assert results[1] == results[2] == results[4] == results[8]
    assert results[1][0], "sample must contain conflicts"


def test_task_order_normalized_by_stable_key():
    rules, task_doc = scenario_concurrency()
    tasks = [TaskSpec(task_id=t["task_id"], target=t["target"],
                      rule_ids=tuple(t["rule_ids"]))
             for t in task_doc["tasks"]]
    batch = _run(rules, tasks, concurrency=4)
    task_ids = [o.task_id for o in batch.outcomes]
    assert task_ids == sorted(task_ids)


def test_queue_full_rejects_enqueue_with_business_code():
    rules, task_doc = scenario_queue_full()
    tasks = [TaskSpec(task_id=t["task_id"], target=t["target"],
                      rule_ids=tuple(t["rule_ids"]), simulate=t.get("simulate"))
             for t in task_doc["tasks"]]
    batch = _run(rules, tasks, concurrency=1, queue_limit=1,
                 task_timeout_sec=30.0, enqueue_grace_sec=0.02)
    rejected = [o for o in batch.outcomes if o.status == "rejected_queue"]
    assert rejected, "saturated queue must reject enqueue attempts"
    assert all(o.code == errors.E_QUEUE_FULL for o in rejected)
    # Rejections are recorded, not silently dropped.
    assert len(batch.outcomes) == len(tasks)


def test_summary_cross_consistent_with_details():
    rules, task_doc = scenario_concurrency()
    tasks = [TaskSpec(task_id=t["task_id"], target=t["target"],
                      rule_ids=tuple(t["rule_ids"]))
             for t in task_doc["tasks"]]
    batch = _run(rules, tasks, concurrency=3)
    summary = build_summary(batch.outcomes)
    assert summary["total"] == len(batch.outcomes)
    assert summary["succeeded"] + summary["resource_limited"] \
        + summary["failed"] + summary["rejected_queue"] \
        + summary["not_run"] == summary["total"]


def test_report_summary_matches_task_details():
    rules, task_doc = scenario_concurrency()
    tasks = [TaskSpec(task_id=t["task_id"], target=t["target"],
                      rule_ids=tuple(t["rule_ids"]))
             for t in task_doc["tasks"]]
    batch = _run(rules, tasks, concurrency=2)
    report = build_report(
        batch_id="b1", policy_fingerprint="fp", config_snapshot={},
        rejections=[], outcomes=batch.outcomes, cycles=[],
        events=batch.events, conclusion=batch.conclusion,
        budget_consumed_bytes=batch.budget_consumed_bytes)
    assert report["summary"]["total"] == len(report["tasks"])
    assert report["summary"]["succeeded"] == sum(
        1 for t in report["tasks"] if t["status"] == "succeeded")


def test_planned_tasks_group_by_target():
    rules, _ = scenario_concurrency()
    parsed = parse_policies({"policy_version": "1.0.0", "rules": rules})
    tasks = plan_tasks(parsed.rules)
    assert len(tasks) == 6
    assert all(t.task_id == f"group:{t.target}" for t in tasks)
