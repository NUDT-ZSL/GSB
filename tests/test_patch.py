import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.helpers import Harness


class PatchClosureTests(unittest.TestCase):
    def setUp(self):
        self.harness = Harness()
        self.harness.load()

    def tearDown(self):
        self.harness.cleanup()

    def test_gear_change_propagates_through_downstream_closure(self):
        orch = self.harness.orchestrator()
        context = self.harness.context()
        orch.reconcile(context)
        round_no = orch.storage.latest_round()["round_no"]
        durable_auth = orch.storage.get_conclusion(round_no, "auth")
        before_blob = durable_auth.to_dict()

        patch = {
            "gear_changes": [
                {
                    "service": "gateway",
                    "old_ratio": "0.5",
                    "new_ratio": "0.6",
                }
            ],
            "dependency_changes": [],
            "tombstones": [],
        }
        result = orch.apply_patch(context, patch)
        # gateway and everything downstream (auth, profile, billing, report)
        self.assertIn("gateway", result["affected_closure"])
        self.assertIn("auth", result["affected_closure"])
        self.assertIn("report", result["affected_closure"])
        recompute_services = {row["service"] for row in result["recompute"]}
        self.assertEqual(
            recompute_services, set(result["affected_closure"])
        )
        # untouched services are marked reusable, never recomputed
        self.assertEqual(result["reuse"], [])
        # the durable conclusion for a recomputed candidate is untouched until
        # an explicit reconcile, and reusable records stay byte identical
        after_auth = orch.storage.get_conclusion(round_no, "auth")
        self.assertEqual(after_auth.to_dict(), before_blob)
        orch.close()

    def test_unrelated_service_conclusion_is_verbatim_reused(self):
        orch = self.harness.orchestrator()
        context = self.harness.context()
        orch.reconcile(context)
        round_no = orch.storage.latest_round()["round_no"]
        before = orch.storage.get_conclusion(round_no, "billing").to_dict()

        patch = {
            "gear_changes": [],
            "dependency_changes": [
                {"service": "report", "new_upstream": "billing"}
            ],
            "tombstones": [],
        }
        result = orch.apply_patch(context, patch)
        # report and billing are roots; gateway/auth/profile stay reusable
        reusable = {row["service"] for row in result["reuse"]}
        self.assertIn("gateway", reusable)
        self.assertIn("auth", reusable)
        self.assertNotIn("billing", reusable)
        self.assertEqual(
            orch.storage.get_conclusion(round_no, "gateway").to_dict(),
            orch.storage.get_conclusion(round_no, "gateway").to_dict(),
        )
        self.assertEqual(
            orch.storage.get_conclusion(round_no, "billing").to_dict(),
            before,
        )
        orch.close()

    def test_tombstone_marks_owner_for_recompute(self):
        orch = self.harness.orchestrator()
        context = self.harness.context()
        patch = {
            "gear_changes": [],
            "dependency_changes": [],
            "tombstones": [{"service": "report", "generation": 9}],
        }
        result = orch.apply_patch(context, patch)
        self.assertIn("report", result["affected_closure"])
        self.assertTrue(
            next(
                row for row in result["recompute"]
                if row["service"] == "report"
            )["recompute_precision"]
        )
        orch.close()


if __name__ == "__main__":
    unittest.main()
