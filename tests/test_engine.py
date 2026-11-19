from __future__ import annotations

import unittest

from vmpool.engine import apply_request, initial_state, verify_state
from vmpool.errors import E_POOL_QUOTA, E_BUDGET, E_SAFETY_MARGIN
from vmpool.fixed import canonical_json, digest_json
from vmpool.model import load_sample

from .helpers import SAMPLE


class EngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.scenario = load_sample(SAMPLE)

    def run_all(self):
        state = initial_state(self.scenario)
        results = {}
        for request in self.scenario.requests:
            result = apply_request(self.scenario, state, request)
            state = result.state
            results[request.seq] = result
        return state, results

    def test_accept_reject_and_shortage_matrix(self) -> None:
        state, results = self.run_all()
        self.assertEqual(results[1].decision["status"], "accepted")
        self.assertEqual(results[1].state["services"]["db"]["replicas"], 5)
        self.assertEqual(results[5].decision["status"], "rejected")
        self.assertEqual(results[5].decision["reason_code"], E_BUDGET)
        self.assertEqual(results[5].decision["first_constraint"], "budget")
        self.assertEqual(results[11].decision["status"], "rejected")
        self.assertEqual(results[11].decision["reason_code"], E_SAFETY_MARGIN)
        self.assertEqual(
            results[11].decision["first_constraint"], "service_cpu_safety"
        )
        self.assertEqual(results[13].decision["status"], "rejected")
        self.assertEqual(results[13].decision["reason_code"], E_POOL_QUOTA)
        self.assertEqual(results[13].decision["first_constraint"], "pool_cpu")
        self.assertTrue(results[6].decision["is_reversal"])
        self.assertEqual(
            results[6].decision["reversal_kind"], "target_reversal"
        )
        self.assertTrue(results[8].decision["is_reversal"])
        self.assertTrue(results[5].decision["precision_sensitive"])
        self.assertTrue(results[11].decision["precision_sensitive"])
        self.assertEqual(
            results[12].decision["shortage_reason_code"],
            "E_DEPENDENCY_CAPACITY",
        )
        self.assertEqual(state["version"], 13)
        verification = verify_state(self.scenario, state)
        self.assertTrue(verification["ok"], verification["errors"])

    def test_incremental_reuse_and_exact_closure(self) -> None:
        state = initial_state(self.scenario)
        initial_cache = canonical_json(state["derived"]["cache"])
        initial_api = canonical_json(state["derived"]["api"])
        initial_edge = canonical_json(state["derived"]["edge"])

        result1 = apply_request(self.scenario, state, self.scenario.requests[0])
        state = result1.state
        self.assertEqual(result1.affected_services, ["db"])
        self.assertEqual(
            set(result1.reused_services), {"cache", "api", "edge"}
        )
        self.assertEqual(canonical_json(state["derived"]["cache"]), initial_cache)
        self.assertEqual(canonical_json(state["derived"]["api"]), initial_api)
        self.assertEqual(canonical_json(state["derived"]["edge"]), initial_edge)

        result2 = apply_request(self.scenario, state, self.scenario.requests[1])
        state = result2.state
        self.assertEqual(result2.affected_services, ["api"])
        self.assertEqual(result2.invalidations, [])
        self.assertEqual(
            canonical_json(state["derived"]["edge"]), initial_edge
        )

        result3 = apply_request(self.scenario, state, self.scenario.requests[2])
        self.assertEqual(
            result3.affected_services, ["cache", "api", "edge"]
        )
        self.assertEqual(
            [item["target_service"] for item in result3.invalidations],
            ["cache", "api", "edge"],
        )
        self.assertEqual(
            result3.invalidations[2]["propagation_path"],
            ["cache", "api", "edge"],
        )

    def test_withdraw_and_reversal_invalidate_downstream_closure(self) -> None:
        state = initial_state(self.scenario)
        for request in self.scenario.requests[:6]:
            state = apply_request(self.scenario, state, request).state
        withdraw = apply_request(
            self.scenario, state, self.scenario.requests[6]
        )
        self.assertEqual(withdraw.decision["status"], "accepted")
        self.assertTrue(withdraw.decision["is_reversal"])
        self.assertEqual(withdraw.decision["reversal_kind"], "withdraw")
        self.assertEqual(
            withdraw.affected_services, ["db", "cache", "api", "edge"]
        )
        self.assertEqual(
            [item["target_service"] for item in withdraw.invalidations],
            ["db", "cache", "api", "edge"],
        )
        self.assertEqual(
            withdraw.invalidations[3]["propagation_path"],
            ["db", "api", "edge"],
        )
        self.assertTrue(
            all(
                item["source_request_seq"] == 7
                for item in withdraw.invalidations
            )
        )

    def test_two_fresh_runs_are_bit_identical(self) -> None:
        state1, results1 = self.run_all()
        state2, results2 = self.run_all()
        self.assertEqual(canonical_json(state1), canonical_json(state2))
        self.assertEqual(digest_json(state1), digest_json(state2))
        for seq in results1:
            self.assertEqual(
                canonical_json(results1[seq].decision),
                canonical_json(results2[seq].decision),
            )


if __name__ == "__main__":
    unittest.main()
