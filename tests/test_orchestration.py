from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from lockfair.engine import LockFairEngine
from lockfair.errors import BusinessError, ErrorCode

from tests.helpers import application, holder, make_bundle, resource


class OrchestrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "state.sqlite"
        self.engine = LockFairEngine(self.db)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_budget_is_hard_cap(self) -> None:
        raw = make_bundle(
            budget=1000,
            quota=1,
            labels=["gold"],
            resources=[resource("a", priority=30), resource("b", priority=20), resource("c", priority=10)],
            holders=[holder("alice", 3)],
            applications=[
                application(1, "alice", "a", desired=500),
                application(2, "alice", "b", desired=500),
                application(3, "alice", "c", desired=500),
            ],
        )
        self.engine.initialize(raw, force=True)
        snapshot = self.engine.execute_round()
        summary = snapshot["summary"]
        self.assertEqual(len(summary["grants"]), 2)
        self.assertEqual(summary["used_budget_ms"], 1000)
        self.assertLessEqual(summary["used_budget_ms"], summary["budget_ms"])
        self.assertEqual(len(summary["deferred"]), 1)
        self.assertEqual(summary["deferred"][0]["first_trigger"], "budget")
        self.assertTrue(summary["fairness"]["satisfied"])

    def test_holder_cap_serializes_same_holder(self) -> None:
        raw = make_bundle(
            budget=2000,
            quota=1,
            labels=["gold"],
            resources=[resource("a", priority=20), resource("b", priority=90)],
            holders=[holder("bob", 1)],
            applications=[
                application(1, "bob", "a", label="gold", priority=5),
                application(2, "bob", "b", label="gold", priority=100),
            ],
        )
        self.engine.initialize(raw, force=True)
        snapshot = self.engine.execute_round()
        summary = snapshot["summary"]
        self.assertEqual([grant["seq"] for grant in summary["grants"]], [2, 1])
        grants = sorted(summary["grants"], key=lambda item: item["start_clock_ms"])
        for previous, current in zip(grants, grants[1:]):
            self.assertGreaterEqual(current["start_clock_ms"], previous["expires_at_ms"])
        self.assertLessEqual(snapshot["state"]["holders"]["bob"]["occupied_count"], 1)
        self.assertLessEqual(
            snapshot["state"]["holders"]["bob"]["occupied_count"],
            snapshot["state"]["holders"]["bob"]["max_concurrent_grants"],
        )

    def test_fairness_quota_and_label_rotation(self) -> None:
        raw = make_bundle(
            budget=1200,
            quota=2,
            labels=["gold", "silver", "bronze"],
            resources=[resource("fair-lane", duration=300, priority=50)],
            holders=[holder("alice", 3)],
            applications=[
                application(1, "alice", "fair-lane", label="gold", priority=100, desired=300),
                application(2, "alice", "fair-lane", label="silver", priority=1, desired=300),
                application(3, "alice", "fair-lane", label="bronze", priority=1, desired=300),
            ],
        )
        self.engine.initialize(raw, force=True)
        snapshot = self.engine.execute_round()
        summary = snapshot["summary"]
        labels = {grant["fairness_label"] for grant in summary["grants"]}
        self.assertEqual(labels, {"gold", "silver", "bronze"})
        self.assertGreaterEqual(len(summary["grants"]), 2)
        self.assertTrue(summary["fairness"]["satisfied"])
        self.assertLessEqual(summary["used_budget_ms"], 1200)

    def test_starvation_forced_yield_compares_high_priority_newcomer(self) -> None:
        raw = make_bundle(
            budget=1000,
            quota=1,
            starvation=2,
            labels=["gold", "bronze"],
            resources=[resource("slow-lane", duration=1000, priority=10, max_wait=10)],
            holders=[holder("alice", 2), holder("bob", 2)],
            applications=[
                application(1, "alice", "slow-lane", label="bronze", priority=1, desired=1000),
                application(2, "bob", "slow-lane", label="gold", priority=100, desired=1000),
                application(3, "alice", "slow-lane", arrival=1100, label="gold", priority=100, desired=1000),
                application(4, "bob", "slow-lane", arrival=2100, label="gold", priority=100, desired=1000),
            ],
        )
        self.engine.initialize(raw, force=True)
        round1 = self.engine.execute_round()
        self.assertEqual([grant["seq"] for grant in round1["summary"]["grants"]], [2])
        self.assertEqual(round1["state"]["applications"]["1"]["wait_rounds"], 1)

        # A new engine instance proves starvation data came back from SQLite.
        restarted = LockFairEngine(self.db)
        round2 = restarted.execute_round()
        self.assertEqual([grant["seq"] for grant in round2["summary"]["grants"]], [3])
        self.assertEqual(round2["state"]["applications"]["1"]["wait_rounds"], 2)

        restarted = LockFairEngine(self.db)
        round3 = restarted.execute_round()
        summary = round3["summary"]
        self.assertEqual([grant["seq"] for grant in summary["grants"]], [1])
        self.assertTrue(summary["grants"][0]["forced_yield"])
        decision = summary["forced_yields"][0]
        self.assertEqual(decision["seq"], 1)
        self.assertEqual(decision["threshold"], 2)
        self.assertEqual(decision["compared_application_seq"], 4)
        self.assertGreaterEqual(decision["estimated_avoided_delay_rounds"], 1)
        self.assertEqual(round3["state"]["applications"]["1"]["forced_yields"], 1)
        self.assertLessEqual(
            round3["state"]["applications"]["1"]["forced_yields"],
            raw["config"]["max_forced_yields_per_application"],
        )

    def test_lease_generation_and_atomic_release(self) -> None:
        raw = make_bundle(
            budget=2000,
            quota=1,
            starvation=4,
            labels=["gold", "silver", "bronze"],
            resources=[resource("recycle", duration=500, generation=10)],
            holders=[holder("alice", 2), holder("bob", 2), holder("carol", 2)],
            applications=[
                application(1, "alice", "recycle", label="gold", priority=100, expected_generation=10),
                application(2, "bob", "recycle", label="silver", priority=90),
                application(3, "carol", "recycle", label="bronze", priority=80, expected_generation=10),
            ],
        )
        self.engine.initialize(raw, force=True)
        snapshot = self.engine.execute_round()
        summary = snapshot["summary"]
        generations = [grant["lease_generation"] for grant in summary["grants"]]
        self.assertEqual(generations, [11, 12])
        self.assertEqual(len(generations), len(set(generations)))
        self.assertEqual(snapshot["state"]["resources"]["recycle"]["lease_generation"], 12)
        self.assertEqual(snapshot["state"]["holders"]["alice"]["occupied_count"], 0)
        self.assertEqual(snapshot["state"]["holders"]["bob"]["occupied_count"], 0)
        stale = [issue for issue in summary["errors"] if issue["code"] == ErrorCode.STALE_LEASE_GENERATION.value]
        self.assertEqual(len(stale), 1)
        self.assertEqual(stale[0]["application_seq"], 3)
        for holder_state in snapshot["state"]["holders"].values():
            self.assertGreaterEqual(holder_state["occupied_count"], 0)
            self.assertLessEqual(
                holder_state["occupied_count"],
                holder_state["max_concurrent_grants"],
            )

    def test_per_key_error_does_not_block_other_key(self) -> None:
        raw = make_bundle(
            budget=2000,
            quota=1,
            labels=["gold"],
            resources=[resource("stale-key", generation=5), resource("healthy-key", generation=2)],
            holders=[holder("alice", 2), holder("bob", 2)],
            applications=[
                application(1, "alice", "stale-key", expected_generation=4),
                application(2, "bob", "healthy-key", priority=100),
            ],
        )
        self.engine.initialize(raw, force=True)
        snapshot = self.engine.execute_round()
        summary = snapshot["summary"]
        self.assertEqual([grant["lock_key"] for grant in summary["grants"]], ["healthy-key"])
        self.assertEqual([issue["code"] for issue in summary["errors"]], [ErrorCode.STALE_LEASE_GENERATION.value])
        self.assertEqual(snapshot["version"], 2)

    def test_budget_cannot_satisfy_any_candidate(self) -> None:
        raw = make_bundle(
            budget=100,
            quota=0,
            labels=["gold"],
            resources=[resource("expensive", duration=1000, max_wait=6)],
            holders=[holder("alice", 1)],
            applications=[application(1, "alice", "expensive", desired=1000)],
        )
        self.engine.initialize(raw, force=True)
        snapshot = self.engine.execute_round()
        codes = [issue["code"] for issue in snapshot["summary"]["errors"]]
        self.assertIn(ErrorCode.BUDGET_CANNOT_SATISFY_ANY.value, codes)
        error_item = next(item for item in snapshot["summary"]["errors"] if item["code"] == ErrorCode.BUDGET_CANNOT_SATISFY_ANY.value)
        self.assertEqual(error_item["lock_key"], "expensive")
        self.assertEqual(error_item["constraint"], "budget")

    def test_clock_rollback_code_and_no_commit(self) -> None:
        raw = make_bundle(
            budget=1000,
            quota=1,
            labels=["gold"],
            resources=[resource("clock-lane")],
            holders=[holder("alice", 1)],
            applications=[application(1, "alice", "clock-lane")],
            rollback_tolerance=10,
        )
        self.engine.initialize(raw, force=True)
        self.engine.execute_round()
        before = self.engine.get_state()
        with self.assertRaises(BusinessError) as captured:
            self.engine.execute_round(clock_ms=100, round_no=2)
        self.assertEqual(captured.exception.code, ErrorCode.CLOCK_ROLLBACK_EXCEEDED)
        self.assertEqual(captured.exception.constraint, "logical_clock")
        after = self.engine.get_state()
        self.assertEqual(before["version"], after["version"])
        self.assertEqual(after["round_no"], 1)

    def test_cancel_records_position_and_wait(self) -> None:
        raw = make_bundle(
            budget=1000,
            quota=1,
            labels=["gold", "silver"],
            resources=[resource("cancel-lane")],
            holders=[holder("alice", 1), holder("bob", 1)],
            applications=[
                application(1, "alice", "cancel-lane", label="gold", priority=100),
                application(2, "bob", "cancel-lane", label="silver", priority=90),
            ],
        )
        self.engine.initialize(raw, force=True)
        result = self.engine.cancel_application(2, reason="operator_test")
        self.assertEqual(result["application"]["status"], "cancelled")
        self.assertIsNotNone(result["event"]["queue_position"])
        self.assertEqual(result["event"]["details"]["reason"], "operator_test")
        self.assertEqual(self.engine.get_state()["version"], 2)
        self.assertTrue(any(event["event_type"] == "cancelled" for event in self.engine.list_queue_events()))

    def test_replaying_committed_round_returns_round_snapshot_after_mutation(self) -> None:
        raw = make_bundle(
            budget=1000,
            quota=1,
            labels=["gold", "silver"],
            resources=[resource("replay-lane")],
            holders=[holder("alice", 1), holder("bob", 1)],
            applications=[
                application(1, "alice", "replay-lane", label="gold", priority=100),
                application(2, "bob", "replay-lane", label="silver", priority=90),
                application(3, "alice", "replay-lane", arrival=5000, label="gold", priority=80),
            ],
        )
        self.engine.initialize(raw, force=True)
        first = self.engine.execute_round()
        self.engine.cancel_application(3)
        self.assertEqual(self.engine.get_state()["version"], 3)
        replay = self.engine.execute_round(round_no=1)
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(replay["version"], first["version"])
        self.assertEqual(replay["state_hash"], first["state_hash"])


if __name__ == "__main__":
    unittest.main()
