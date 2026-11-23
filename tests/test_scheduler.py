import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scaler.errors import ScalerError
from scaler.placement import NodePool
from scaler.models import Node
from tests.helpers import Harness


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.harness = Harness()
        self.harness.load()

    def tearDown(self):
        self.harness.cleanup()

    def test_full_round_records_every_service_and_passes_verify(self):
        orch = self.harness.orchestrator()
        context = self.harness.context()
        result = orch.reconcile(context)
        services = {row["service"] for row in result["results"]}
        self.assertEqual(
            services, {s.name for s in context.services}
        )
        verification = orch.verify(self.harness.context())
        self.assertTrue(verification["ok"], verification)
        orch.close()

    def test_budget_exhaustion_abandons_remaining_actions(self):
        orch = self.harness.orchestrator()
        context = self.harness.context()
        # only two actions fit: 2 * 150 = 300 <= 300
        context.config["round_budget_ms"] = 300
        result = orch.reconcile(context)
        outcomes = {row["service"]: row for row in result["results"]}
        started = [r for r in result["results"] if r["status"] == "finished"]
        abandoned = [
            r for r in result["results"] if r["status"] == "not_started"
        ]
        self.assertLessEqual(len(started), 2)
        self.assertTrue(abandoned)
        self.assertTrue(
            all(row["outcome"] in {"budget_exhausted", "abandoned"}
                for row in abandoned)
        )
        # every service got exactly one durable action row
        actions = orch.storage.actions(result["round_no"])
        self.assertEqual(len(actions), len(context.services))
        orch.close()

    def test_fd_concurrency_is_enforced(self):
        orch = self.harness.orchestrator()
        context = self.harness.context()
        context.config["fd_concurrency"] = 1
        result = orch.reconcile(context)
        # serial execution still completes everything within the large budget
        self.assertEqual(
            len(result["results"]), len(context.services)
        )
        orch.close()

    def test_placement_is_atomic_and_records_first_constraint(self):
        pool = NodePool([
            Node("n1", 4, 4, 2, 4, "fd"),
            Node("n2", 4, 4, 4, 1, "fd"),
        ])
        # request 4 replicas of (2 cpu, 2 mem): node1 fits 1, node2 blocked
        # on memory after... total feasible only 1 -> atomic rejection
        with self.assertRaises(ScalerError) as ctx:
            pool.place("svc", 4, 2, 2, "fd")
        self.assertEqual(ctx.exception.code, "E_PLACEMENT_UNSATISFIABLE")
        self.assertEqual(ctx.exception.details["first_constraint"], "mem")
        self.assertTrue(ctx.exception.details["rejected_candidates"])
        # nothing was committed
        self.assertEqual(pool.nodes["n1"].cpu_free, 2)
        self.assertEqual(pool.nodes["n2"].mem_free, 1)

    def test_scale_in_then_scale_out_avoids_false_shortage(self):
        # one tight node: scale in frees room before expansion placement
        pool = NodePool([Node("n1", 4, 4, 2, 2, "fd")])
        pool.occupancy["n1"]["a"] = 1
        released = pool.release(
            "a", {"cpu_per_replica": 2, "mem_per_replica": 2}
        )
        self.assertTrue(released)
        placements = pool.place("b", 2, 2, 2, "fd")
        self.assertEqual(sum(p["replicas"] for p in placements), 2)

    def test_fixed_point_ledger_tracks_rounding_and_sensitivity(self):
        from scaler.fixedpoint import FixedLedger

        ledger = FixedLedger(scale=1000, tolerance=5)
        left = ledger.convert("svc", "0.3333", "x")
        right = ledger.convert("svc", "0.3334", "y")
        self.assertEqual(ledger.entries[0].round_dir, "up")
        self.assertEqual(ledger.entries[0].discarded, "0.3000")
        self.assertTrue(ledger.is_sensitive(left, right))
        chain = ledger.chain_for(left, right)
        self.assertEqual(chain, [1, 2])
        product = ledger.mul("svc", left, right, "z")
        self.assertGreaterEqual(product.err, 0)


if __name__ == "__main__":
    unittest.main()
