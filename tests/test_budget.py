"""Resource budget tests: timeout, memory limit, batch budget exhaustion."""

from policy_validator import errors
from policy_validator.models import TaskSpec
from policy_validator.parser import parse_policies
from policy_validator.scheduler import SchedulerConfig, run_batch

from sample_gen import scenario_memory, scenario_timeout


def _rules_by_id(rules):
    parsed = parse_policies({"policy_version": "1.0.0", "rules": rules})
    return {r.rule_id: r for r in parsed.rules}


def _tasks(task_doc):
    return [TaskSpec(task_id=t["task_id"], target=t["target"],
                     rule_ids=tuple(t["rule_ids"]), simulate=t.get("simulate"))
            for t in task_doc["tasks"]]


def test_timeout_marks_task_and_keeps_others_running():
    rules, task_doc = scenario_timeout()
    config = SchedulerConfig(concurrency=2, task_timeout_sec=0.2)
    batch = run_batch(_tasks(task_doc), _rules_by_id(rules), config)
    by_id = {o.task_id: o for o in batch.outcomes}
    slow = by_id["group:slow.lane"]
    assert slow.status == "timeout"
    assert slow.code == errors.E_TASK_TIMEOUT
    assert slow.elapsed_ms >= 200  # measured, not declared
    assert by_id["group:fast.lane"].status == "succeeded"
    assert batch.conclusion == "completed"


def test_memory_limit_marks_task_and_keeps_others_running():
    rules, task_doc = scenario_memory()
    config = SchedulerConfig(concurrency=2,
                             task_memory_limit_bytes=8 * 1024 * 1024,
                             batch_memory_budget_bytes=1 << 40)
    batch = run_batch(_tasks(task_doc), _rules_by_id(rules), config)
    by_id = {o.task_id: o for o in batch.outcomes}
    big = by_id["group:big.job"]
    assert big.status == "memory_exceeded"
    assert big.code == errors.E_TASK_MEMORY_EXCEEDED
    assert big.mem_bytes >= 32 * 1024 * 1024  # actual allocation measured
    assert by_id["group:small.job"].status == "succeeded"


def test_batch_budget_exhaustion_stops_scheduling():
    rules, task_doc = scenario_memory()
    tasks = _tasks(task_doc)
    # Budget smaller than one big allocation: after the big task completes
    # the cumulative budget is exhausted and nothing else may be scheduled.
    config = SchedulerConfig(concurrency=1,
                             queue_limit=1,
                             enqueue_grace_sec=5.0,
                             task_memory_limit_bytes=1 << 40,
                             batch_memory_budget_bytes=16 * 1024 * 1024)
    batch = run_batch(tasks, _rules_by_id(rules), config)
    assert batch.conclusion == "budget_exhausted"
    assert any(e["code"] == errors.E_BATCH_BUDGET_EXHAUSTED
               for e in batch.events)
    statuses = {o.task_id: o.status for o in batch.outcomes}
    assert statuses["group:big.job"] == "succeeded"  # under per-task limit
    assert statuses["group:small.job"] == "not_run"
    not_run = [o for o in batch.outcomes if o.status == "not_run"]
    assert all(o.code == errors.E_BATCH_BUDGET_EXHAUSTED for o in not_run)
