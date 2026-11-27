from __future__ import annotations

import copy
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout

from pub_scheduler import SchedulerEngine, generate_workload, workload_digest
from pub_scheduler.audit import verify_result
from pub_scheduler.checkpoint import (
    CheckpointError,
    load_checkpoint,
    save_checkpoint,
)
from pub_scheduler.engine import compute_urgency
from pub_scheduler.cli import main as cli_main
from pub_scheduler.model import (
    CATEGORIES,
    DIMENSIONS,
    SCHEMA_VERSION,
    vector_add,
    validate_workload,
)


def node(
    node_id: str,
    capacity: dict[str, int],
    categories: list[str],
) -> dict:
    return {
        "node_id": node_id,
        "capacity": copy.deepcopy(capacity),
        "categories": list(categories),
    }


def job(
    job_id: str,
    *,
    category: str = "章节渲染",
    priority: int = 3,
    preemptible: bool = True,
    demand: dict[str, int] | None = None,
    duration: int = 5,
    deadline: int = 20,
    submit_time: int = 0,
    mutex_group: str | None = None,
    downgrade_plan: dict | None = None,
) -> dict:
    if demand is None:
        demand = {"cpu": 2, "memory": 2, "io": 2}
    code = {
        "章节渲染": "chapter_render",
        "音视频转码": "av_transcode",
        "索引重建": "index_rebuild",
        "预热预取": "prefetch_warmup",
    }[category]
    return {
        "job_id": job_id,
        "name": job_id,
        "category": category,
        "category_code": code,
        "priority": priority,
        "preemptible": preemptible,
        "demand": copy.deepcopy(demand),
        "duration": duration,
        "deadline": deadline,
        "submit_time": submit_time,
        "mutex_group": mutex_group,
        "downgrade_plan": copy.deepcopy(downgrade_plan),
    }


def workload(
    nodes: list[dict],
    jobs: list[dict],
    cancellations: list[tuple[str, int]] | None = None,
) -> dict:
    raw_events = []
    for item in jobs:
        raw_events.append((item["submit_time"], 1, "submit", item["job_id"]))
    for job_id, at in cancellations or []:
        raw_events.append((at, 0, "cancel", job_id))
    raw_events.sort(key=lambda event: (event[0], event[1], event[3]))
    events = [
        {
            "event_id": f"E{index + 1:04d}",
            "at": at,
            "sequence": index,
            "type": event_type,
            "job_id": job_id,
        }
        for index, (at, _, event_type, job_id) in enumerate(raw_events)
    ]
    return validate_workload(
        {
            "schema_version": SCHEMA_VERSION,
            "seed": 0,
            "nodes": nodes,
            "jobs": jobs,
            "events": events,
        }
    )


class GeneratorTests(unittest.TestCase):
    def test_fixed_seed_is_reconstructible_and_complete(self) -> None:
        first = generate_workload(77)
        second = generate_workload(77)
        self.assertEqual(first, second)
        self.assertEqual(workload_digest(first), workload_digest(second))
        self.assertEqual(
            json.loads(json.dumps(first, ensure_ascii=False)), first
        )
        self.assertEqual(first["generator"]["seed"], 77)
        self.assertEqual(
            {item["category"] for item in first["jobs"]}, set(CATEGORIES)
        )
        self.assertTrue(
            all(1 <= item["priority"] <= 5 for item in first["jobs"])
        )
        self.assertTrue(
            any(event["type"] == "cancel" for event in first["events"])
        )
        event_keys = [
            (event["at"], event["sequence"]) for event in first["events"]
        ]
        self.assertEqual(event_keys, sorted(event_keys))
        self.assertEqual(len(event_keys), len(set(event_keys)))
        for item in first["jobs"]:
            plan = item["downgrade_plan"]
            if plan is not None:
                expected = {
                    dimension: item["demand"][dimension]
                    - plan["demand"][dimension]
                    for dimension in DIMENSIONS
                }
                self.assertEqual(plan["release"], expected)

    def test_demo_seed_exercises_all_decision_types(self) -> None:
        engine = SchedulerEngine(generate_workload(3)).run_to_end()
        types = {decision["type"] for decision in engine.decisions}
        self.assertEqual(
            types, {"ADMIT", "PREEMPT", "DOWNGRADE", "REJECT"}
        )


class CapacityBoundaryTests(unittest.TestCase):
    def test_exact_capacity_is_deterministic_admission(self) -> None:
        exact = {"cpu": 4, "memory": 8, "io": 2}
        sample = workload(
            [node("n1", exact, ["章节渲染"])],
            [
                job(
                    "exact",
                    priority=5,
                    preemptible=False,
                    demand=exact,
                    duration=4,
                    deadline=10,
                )
            ],
        )
        first = SchedulerEngine(sample).run_to_end()
        second = SchedulerEngine(sample).run_to_end()
        self.assertEqual(first.decisions, second.decisions)
        self.assertEqual(first.capacity_changes, second.capacity_changes)
        self.assertEqual(len(first.decisions), 1)
        self.assertEqual(first.decisions[0]["type"], "ADMIT")
        first_snapshot_node = first.snapshots[0]["nodes"][0]
        self.assertEqual(
            first_snapshot_node["free"], {"cpu": 0, "memory": 0, "io": 0}
        )
        self.assertEqual(
            first_snapshot_node["used"], exact
        )

    def test_downgrade_release_exactly_meets_demand(self) -> None:
        plan = {
            "profile": "eco",
            "demand": {"cpu": 2, "memory": 2, "io": 2},
            "release": {"cpu": 2, "memory": 2, "io": 2},
        }
        sample = workload(
            [
                node(
                    "n1",
                    {"cpu": 10, "memory": 10, "io": 10},
                    ["章节渲染"],
                )
            ],
            [
                job(
                    "low",
                    priority=2,
                    demand={"cpu": 4, "memory": 4, "io": 4},
                    duration=20,
                    deadline=30,
                    downgrade_plan=plan,
                ),
                job(
                    "high",
                    priority=5,
                    preemptible=False,
                    demand={"cpu": 8, "memory": 8, "io": 8},
                    duration=2,
                    deadline=10,
                    submit_time=1,
                ),
                job(
                    "follower",
                    priority=1,
                    preemptible=False,
                    demand={"cpu": 1, "memory": 1, "io": 1},
                    duration=1,
                    deadline=5,
                    submit_time=1,
                ),
            ],
        )
        engine = SchedulerEngine(sample).run_until(1)
        at_one = [
            decision
            for decision in engine.decisions
            if decision["at"] == 1
        ]
        self.assertEqual(
            [decision["type"] for decision in at_one],
            ["DOWNGRADE", "ADMIT", "REJECT"],
        )
        self.assertEqual(at_one[0]["job_id"], "low")
        self.assertEqual(at_one[0]["released"], plan["release"])
        self.assertEqual(at_one[1]["job_id"], "high")
        self.assertEqual(at_one[2]["job_id"], "follower")
        self.assertEqual(
            engine.job_state["low"]["current_alloc"],
            {"cpu": 2, "memory": 2, "io": 2},
        )
        self.assertEqual(engine.job_state["follower"]["status"], "rejected")
        snapshot = next(item for item in engine.snapshots if item["at"] == 1)
        self.assertEqual(
            snapshot["nodes"][0]["free"],
            {"cpu": 0, "memory": 0, "io": 0},
        )
        engine.run_to_end()
        self.assertEqual(engine.check_invariants(), [])


class UrgencyTests(unittest.TestCase):
    def test_expired_state_has_distinct_downgraded_semantics(self) -> None:
        item = job("u", priority=5, deadline=5, submit_time=0)
        active = compute_urgency(item, 4)
        exact_deadline = compute_urgency(item, 5)
        overdue = compute_urgency(item, 6)
        self.assertEqual(active["state"], "ACTIVE")
        self.assertGreater(active["urgency_score"], 0)
        for row in (exact_deadline, overdue):
            self.assertEqual(row["state"], "EXPIRED")
            self.assertEqual(row["effective_priority"], 0)
            self.assertLess(row["urgency_score"], 0)
            self.assertEqual(row["sort_key"][0], -0)
        self.assertLess(overdue["urgency_score"], exact_deadline["urgency_score"])
        self.assertNotEqual(active["sort_key"], exact_deadline["sort_key"])

    def test_shorter_remaining_and_higher_priority_sort_first(self) -> None:
        short = job("short", priority=4, deadline=10)
        long = job("long", priority=4, deadline=20)
        high = job("high", priority=5, deadline=20)
        rows = [
            compute_urgency(item, 0) for item in (long, short, high)
        ]
        rows.sort(key=lambda row: row["sort_key"])
        self.assertEqual(
            [row["job_id"] for row in rows], ["high", "short", "long"]
        )


class ContentionTests(unittest.TestCase):
    def test_migration_precedes_preemption_when_target_fits(self) -> None:
        sample = workload(
            [
                node(
                    "node-a",
                    {"cpu": 10, "memory": 10, "io": 10},
                    ["章节渲染"],
                ),
                node(
                    "node-b",
                    {"cpu": 5, "memory": 5, "io": 5},
                    ["章节渲染"],
                ),
            ],
            [
                job(
                    "low",
                    priority=1,
                    demand={"cpu": 4, "memory": 4, "io": 4},
                    duration=20,
                    deadline=30,
                ),
                job(
                    "high",
                    priority=5,
                    preemptible=False,
                    demand={"cpu": 8, "memory": 8, "io": 8},
                    duration=2,
                    deadline=10,
                    submit_time=1,
                ),
            ],
        )
        engine = SchedulerEngine(sample).run_until(1)
        at_one = [
            decision
            for decision in engine.decisions
            if decision["at"] == 1
        ]
        self.assertEqual(at_one[0]["type"], "PREEMPT")
        self.assertEqual(at_one[0]["action"], "MIGRATE")
        self.assertEqual(at_one[0]["job_id"], "low")
        self.assertEqual(at_one[0]["target_node_id"], "node-b")
        self.assertEqual(at_one[1]["type"], "ADMIT")
        self.assertEqual(engine.job_state["low"]["current_node_id"], "node-b")
        self.assertEqual(engine.job_state["high"]["current_node_id"], "node-a")
        engine.run_to_end()
        self.assertEqual(engine.check_invariants(), [])

    def test_preemption_and_deadline_trace_include_competitor(self) -> None:
        sample = workload(
            [
                node(
                    "n1",
                    {"cpu": 10, "memory": 10, "io": 10},
                    ["章节渲染"],
                )
            ],
            [
                job(
                    "low",
                    priority=1,
                    demand={"cpu": 4, "memory": 4, "io": 4},
                    duration=20,
                    deadline=30,
                ),
                job(
                    "high",
                    priority=5,
                    preemptible=False,
                    demand={"cpu": 8, "memory": 8, "io": 8},
                    duration=2,
                    deadline=10,
                    submit_time=1,
                ),
            ],
        )
        engine = SchedulerEngine(sample).run_to_end()
        preemptions = [
            decision
            for decision in engine.decisions
            if decision["type"] == "PREEMPT"
        ]
        self.assertEqual(len(preemptions), 1)
        self.assertEqual(preemptions[0]["action"], "EVICT")
        self.assertEqual(preemptions[0]["job_id"], "low")
        self.assertEqual(preemptions[0]["requesting_job_id"], "high")
        misses = engine.summarize()["deadline_misses"]
        low_miss = next(item for item in misses if item["job_id"] == "low")
        self.assertEqual(low_miss["miss_reason"], "PREEMPTED")
        self.assertIn(preemptions[0]["decision_id"], low_miss["chain_decision_ids"])
        self.assertIn("high", low_miss["competition_job_ids"])

    def test_mutex_group_never_shares_a_node(self) -> None:
        sample = workload(
            [
                node(
                    "n1",
                    {"cpu": 20, "memory": 20, "io": 20},
                    ["章节渲染"],
                )
            ],
            [
                job(
                    "first",
                    priority=5,
                    preemptible=False,
                    demand={"cpu": 5, "memory": 5, "io": 5},
                    duration=20,
                    deadline=30,
                    mutex_group="edition-x",
                ),
                job(
                    "second",
                    priority=1,
                    preemptible=False,
                    demand={"cpu": 5, "memory": 5, "io": 5},
                    duration=2,
                    deadline=10,
                    submit_time=1,
                    mutex_group="edition-x",
                ),
                job(
                    "other",
                    priority=2,
                    preemptible=False,
                    demand={"cpu": 5, "memory": 5, "io": 5},
                    duration=2,
                    deadline=10,
                    submit_time=1,
                    mutex_group="edition-y",
                ),
            ],
        )
        engine = SchedulerEngine(sample).run_to_end()
        self.assertEqual(engine.job_state["second"]["status"], "rejected")
        self.assertEqual(engine.job_state["other"]["status"], "completed")
        for snapshot in engine.snapshots:
            for snapshot_node in snapshot["nodes"]:
                groups = [
                    assignment["mutex_group"]
                    for assignment in snapshot_node["active_assignments"]
                    if assignment["mutex_group"] is not None
                ]
                self.assertEqual(len(groups), len(set(groups)))
        self.assertEqual(engine.check_invariants(), [])


class LedgerAndAuditTests(unittest.TestCase):
    def test_generated_run_keeps_ledger_invariant_at_every_sample(self) -> None:
        engine = SchedulerEngine(generate_workload(12345))
        while engine.run_next_time():
            self.assertEqual(engine.check_invariants(), [])
        result = engine.result()
        verification = verify_result(engine.workload, result)
        self.assertTrue(verification["ok"], verification["errors"])
        for snapshot in result["samples"]:
            for snapshot_node in snapshot["nodes"]:
                self.assertEqual(
                    vector_add(snapshot_node["used"], snapshot_node["free"]),
                    snapshot_node["capacity"],
                )
        decision_ids = {
            decision["decision_id"] for decision in result["decisions"]
        }
        for change in result["capacity_changes"]:
            if change["cause"]["kind"] == "decision":
                self.assertIn(change["cause"]["id"], decision_ids)

    def test_audit_detects_tampered_ledger(self) -> None:
        engine = SchedulerEngine(generate_workload(7)).run_to_end()
        result = engine.result()
        result["capacity_changes"][0]["after"]["used"]["cpu"] += 1
        verification = verify_result(engine.workload, result)
        self.assertFalse(verification["ok"])
        self.assertTrue(verification["errors"])


class CheckpointTests(unittest.TestCase):
    def _assert_resume_matches(
        self, checkpoint_time: int, temporary_directory: str
    ) -> None:
        sample = generate_workload(12345)
        full = SchedulerEngine(sample).run_to_end()
        partial = SchedulerEngine(sample).run_until(checkpoint_time)
        path = os.path.join(
            temporary_directory, f"checkpoint-{checkpoint_time}.json"
        )
        save_checkpoint(partial, path)
        resumed = load_checkpoint(path)
        self.assertEqual(resumed.current_time, checkpoint_time)
        resumed.run_to_end()
        self.assertEqual(full.decisions, resumed.decisions)
        self.assertEqual(full.capacity_changes, resumed.capacity_changes)
        self.assertEqual(full.snapshots, resumed.snapshots)
        self.assertEqual(full.urgency_rows, resumed.urgency_rows)
        self.assertEqual(
            full.export_state()["job_state"],
            resumed.export_state()["job_state"],
        )
        later_full = [
            decision
            for decision in full.decisions
            if decision["sequence"] > partial.decision_seq
        ]
        later_resumed = [
            decision
            for decision in resumed.decisions
            if decision["sequence"] > partial.decision_seq
        ]
        self.assertEqual(later_full, later_resumed)
        verification = verify_result(resumed.workload, resumed.result())
        self.assertTrue(verification["ok"], verification["errors"])

    def test_resume_after_event_time_and_gap_time(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            for checkpoint_time in (7, 16):
                with self.subTest(checkpoint_time=checkpoint_time):
                    self._assert_resume_matches(
                        checkpoint_time, temporary_directory
                    )

    def test_checkpoint_digest_and_version_are_checked(self) -> None:
        engine = SchedulerEngine(generate_workload(3)).run_until(7)
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = os.path.join(temporary_directory, "checkpoint.json")
            save_checkpoint(engine, path)
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)

            tampered = copy.deepcopy(payload)
            tampered["content"]["state"]["current_time"] += 1
            tampered_path = os.path.join(temporary_directory, "tampered.json")
            with open(tampered_path, "w", encoding="utf-8") as handle:
                json.dump(tampered, handle, ensure_ascii=False)
            with self.assertRaises(CheckpointError):
                load_checkpoint(tampered_path)

            bad_version = copy.deepcopy(payload)
            bad_version["checkpoint_version"] = "9.9"
            bad_version_path = os.path.join(
                temporary_directory, "bad-version.json"
            )
            with open(bad_version_path, "w", encoding="utf-8") as handle:
                json.dump(bad_version, handle, ensure_ascii=False)
            with self.assertRaises(CheckpointError):
                load_checkpoint(bad_version_path)


class CliTests(unittest.TestCase):
    def test_json_cli_runs_and_audits_successfully(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = cli_main(["--seed", "3", "--json"])
        self.assertEqual(exit_code, 0)
        result = json.loads(output.getvalue())
        self.assertTrue(result["verification"]["ok"])
        self.assertEqual(result["invariant_errors"], [])
        self.assertEqual(
            {decision["type"] for decision in result["decisions"]},
            {"ADMIT", "PREEMPT", "DOWNGRADE", "REJECT"},
        )


if __name__ == "__main__":
    unittest.main()

