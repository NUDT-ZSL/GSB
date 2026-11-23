from __future__ import annotations

import unittest

from gateway.canonical import canonical_json
from gateway.errors import GatewayError
from tests.helpers import (
    config_doc,
    make_engine,
    request,
    request_doc,
    round_command,
    service_doc,
)


class EngineTests(unittest.TestCase):
    def test_generation_and_confirmation_idempotency(self) -> None:
        engine, state = make_engine(
            service_doc(), request_doc([request(1)]), config_doc()
        )
        result = engine.run_round(round_command("d0", 1))
        self.assertEqual(result["started"][0]["generation"], 1)
        self.assertEqual(state["requests"]["1"]["generation"], 1)
        self.assertEqual(state["requests"]["1"]["attempts"][0]["replica_id"], "r1")
        self.assertEqual(state["global_token"]["balance_fixed"], 99000)

        confirm = round_command(
            "c0",
            2,
            outcomes=[
                {
                    "request_id": 1,
                    "generation": 1,
                    "replica_id": "r1",
                    "outcome": "success",
                }
            ],
        )
        first = engine.run_round(confirm)
        self.assertEqual(first["outcomes"][0]["status"], "applied")
        snapshot = canonical_json(state)
        repeated = engine.run_round(confirm)
        self.assertTrue(repeated["idempotent"])
        self.assertEqual(canonical_json(state), snapshot)
        self.assertEqual(state["counters"]["successes"], 1)

        with self.assertRaises(GatewayError) as caught:
            engine.run_round(
                round_command(
                    "wrong-replica",
                    3,
                    outcomes=[
                        {
                            "request_id": 1,
                            "generation": 1,
                            "replica_id": "other",
                            "outcome": "success",
                        }
                    ],
                )
            )
        self.assertEqual(caught.exception.code, "E_CONFIRM_REPLICA_MISMATCH")
        self.assertEqual(canonical_json(state), snapshot)

        no_redispatch = engine.run_round(round_command("d9", 10))
        self.assertEqual(no_redispatch["started"], [])
        self.assertEqual(state["requests"]["1"]["generation"], 1)

    def test_late_and_missing_confirmation(self) -> None:
        engine, state = make_engine(
            service_doc(), request_doc([request(1)]), config_doc()
        )
        engine.run_round(round_command("d0", 1))
        engine.run_round(
            round_command(
                "f0",
                2,
                outcomes=[
                    {
                        "request_id": 1,
                        "generation": 1,
                        "replica_id": "r1",
                        "outcome": "failure",
                    }
                ],
            )
        )
        snapshot = canonical_json(state)
        late = engine.run_round(
            round_command(
                "late",
                2,
                outcomes=[
                    {
                        "request_id": 1,
                        "generation": 1,
                        "replica_id": "r1",
                        "outcome": "success",
                    }
                ],
            )
        )
        self.assertEqual(late["outcomes"][0]["status"], "ignored_late")
        self.assertEqual(
            state["requests"]["1"]["attempts"][0]["status"], "failed"
        )
        self.assertEqual(state["requests"]["1"]["generation"], 1)
        self.assertNotEqual(canonical_json(state), snapshot)

        with self.assertRaises(GatewayError) as caught:
            engine.run_round(
                round_command(
                    "missing",
                    3,
                    outcomes=[
                        {
                            "request_id": 1,
                            "generation": 99,
                            "replica_id": "r1",
                            "outcome": "success",
                        }
                    ],
                )
            )
        self.assertEqual(caught.exception.code, "E_CONFIRM_NOT_FOUND")

    def test_budget_stops_round_and_keeps_queue(self) -> None:
        engine, state = make_engine(
            service_doc(),
            request_doc([request(1, priority=5), request(2, priority=1)]),
            config_doc(),
        )
        result = engine.run_round(round_command("d0", 1, budget_ms=1))
        self.assertEqual(len(result["started"]), 1)
        self.assertEqual(result["started"][0]["request_id"], 1)
        self.assertTrue(result["budget_exhausted"])
        self.assertEqual(result["budget_used_ms"], 1)
        self.assertEqual(result["not_started"][0]["reason"], "budget_exhausted")
        self.assertEqual(state["requests"]["2"]["status"], "queued")
        self.assertEqual(state["counters"]["dispatches"], 1)
        self.assertEqual(state["global_token"]["balance_fixed"], 99000)

    def test_replica_slot_limit(self) -> None:
        engine, state = make_engine(
            service_doc(
                replicas=[{"replica_id": "r1", "slots": 1, "compute_units": 1}]
            ),
            request_doc([request(1), request(2)]),
            config_doc(),
        )
        result = engine.run_round(round_command("d0", 1))
        self.assertEqual(len(result["started"]), 1)
        self.assertEqual(result["not_started"][0]["error_code"], "E_REPLICA_SLOTS_FULL")
        self.assertEqual(state["counters"]["dispatches"], 1)
        attempt = state["requests"]["1"]["attempts"][0]
        self.assertEqual(attempt["status"], "inflight")
        self.assertEqual(state["global_token"]["balance_fixed"], 99000)

    def test_forced_missing_replica_is_isolated(self) -> None:
        engine, state = make_engine(
            service_doc(),
            request_doc([request(1), request(2, priority=2)]),
            config_doc(),
        )
        result = engine.run_round(
            round_command("d0", 1, force_targets={"1": "missing"})
        )
        self.assertEqual(len(result["started"]), 1)
        self.assertEqual(result["started"][0]["request_id"], 2)
        self.assertEqual(result["not_started"][0]["error_code"], "E_REPLICA_NOT_FOUND")
        self.assertIn("request_id", result["not_started"][0])
        self.assertIn("generation", result["not_started"][0])

    def test_backoff_and_dead_letter(self) -> None:
        engine, state = make_engine(
            service_doc(), request_doc([request(1)]), config_doc()
        )
        engine.run_round(round_command("d1", 1))
        failed = engine.run_round(
            round_command(
                "f1",
                2,
                outcomes=[
                    {
                        "request_id": 1,
                        "generation": 1,
                        "replica_id": "r1",
                        "outcome": "failure",
                    }
                ],
            )
        )
        self.assertEqual(failed["failed_after_start"][0]["reason"], "failed")
        request_state = state["requests"]["1"]
        first_next = request_state["next_eligible_ms"]
        self.assertIn(first_next, {4, 5})
        balance_after_failure = state["global_token"]["balance_fixed"]
        dispatches_after_failure = state["counters"]["dispatches"]
        engine.run_round(round_command("noop", 2))
        self.assertEqual(state["global_token"]["balance_fixed"], balance_after_failure)
        self.assertEqual(state["counters"]["dispatches"], dispatches_after_failure)

        engine.run_round(round_command("d2", first_next))
        self.assertEqual(state["requests"]["1"]["generation"], 2)
        second_clock = first_next + 1
        engine.run_round(
            round_command(
                "f2",
                second_clock,
                outcomes=[
                    {
                        "request_id": 1,
                        "generation": 2,
                        "replica_id": "r1",
                        "outcome": "failure",
                    }
                ],
            )
        )
        second_next = state["requests"]["1"]["next_eligible_ms"]
        engine.run_round(round_command("d3", second_next))
        self.assertEqual(state["requests"]["1"]["generation"], 3)
        engine.run_round(
            round_command(
                "f3",
                second_next + 1,
                outcomes=[
                    {
                        "request_id": 1,
                        "generation": 3,
                        "replica_id": "r1",
                        "outcome": "failure",
                    }
                ],
            )
        )
        dead = state["requests"]["1"]["dead_letter"]
        self.assertEqual(dead["trigger"], "failure")
        self.assertEqual(dead["generation"], 3)
        self.assertIn("tombstone_id", dead)
        self.assertEqual(state["counters"]["dead_letters"], 1)
        tombstone_id = dead["tombstone_id"]

        engine.run_round(
            round_command(
                "f3-dup",
                second_next + 1,
                outcomes=[
                    {
                        "request_id": 1,
                        "generation": 3,
                        "replica_id": "r1",
                        "outcome": "failure",
                    }
                ],
            )
        )
        self.assertEqual(state["counters"]["dead_letters"], 1)
        self.assertEqual(
            state["tombstones"]["1"]["tombstone_id"], tombstone_id
        )

    def test_timeout_requires_clock_progress_and_active_abort(self) -> None:
        engine, state = make_engine(
            service_doc(),
            request_doc([request(1)]),
            config_doc(dispatch_timeout_ms=3),
        )
        engine.run_round(round_command("d0", 1))
        attempt = state["requests"]["1"]["attempts"][0]
        self.assertEqual(attempt["deadline_ms"], 4)
        passive = engine.run_round(round_command("late-clock", 10))
        self.assertEqual(passive["recovery"]["expired"], [])
        self.assertEqual(attempt["status"], "inflight")
        self.assertEqual(state["counters"]["expirations"], 0)

        active = engine.run_round(
            round_command("active-abort", 10, abort_overdue=True)
        )
        self.assertEqual(len(active["recovery"]["expired"]), 1)
        self.assertEqual(attempt["status"], "expired")
        self.assertEqual(state["counters"]["expirations"], 1)
        self.assertEqual(state["requests"]["1"]["status"], "retry_wait")

    def test_early_timeout_outcome_is_rejected(self) -> None:
        engine, state = make_engine(
            service_doc(),
            request_doc([request(1)]),
            config_doc(dispatch_timeout_ms=3),
        )
        engine.run_round(round_command("d0", 1))
        snapshot = canonical_json(state)
        with self.assertRaises(GatewayError) as caught:
            engine.run_round(
                round_command(
                    "early",
                    4,
                    outcomes=[
                        {
                            "request_id": 1,
                            "generation": 1,
                            "replica_id": "r1",
                            "outcome": "expired",
                        }
                    ],
                )
            )
        self.assertEqual(caught.exception.code, "E_TIMEOUT_NOT_DUE")
        self.assertEqual(canonical_json(state), snapshot)

    def test_dead_letter_on_expiration(self) -> None:
        engine, state = make_engine(
            service_doc(),
            request_doc([request(1)]),
            config_doc(dispatch_timeout_ms=2, max_generation=1),
        )
        engine.run_round(round_command("d0", 1))
        engine.run_round(round_command("abort", 10, abort_overdue=True))
        dead = state["requests"]["1"]["dead_letter"]
        self.assertEqual(dead["trigger"], "expired")
        self.assertEqual(state["counters"]["dead_letters"], 1)

    def test_dead_letter_on_rejection(self) -> None:
        engine, state = make_engine(
            service_doc(),
            request_doc([request(1)]),
            config_doc(max_generation=1),
        )
        engine.run_round(round_command("d0", 1))
        engine.run_round(
            round_command(
                "reject",
                2,
                outcomes=[
                    {
                        "request_id": 1,
                        "generation": 1,
                        "replica_id": "r1",
                        "outcome": "rejected",
                    }
                ],
            )
        )
        dead = state["requests"]["1"]["dead_letter"]
        self.assertEqual(dead["trigger"], "rejected")
        self.assertEqual(state["counters"]["rejections"], 1)
        self.assertEqual(state["counters"]["dead_letters"], 1)

    def test_abandon_before_deadline_requeues(self) -> None:
        engine, state = make_engine(
            service_doc(),
            request_doc([request(1)]),
            config_doc(dispatch_timeout_ms=10),
        )
        engine.run_round(round_command("d0", 1))
        result = engine.run_round(
            round_command(
                "abandon",
                2,
                outcomes=[
                    {
                        "request_id": 1,
                        "generation": 1,
                        "replica_id": "r1",
                        "outcome": "abort",
                    }
                ],
            )
        )
        self.assertEqual(len(result["abandoned"]), 1)
        self.assertEqual(state["requests"]["1"]["attempts"][0]["status"], "abandoned")
        self.assertEqual(state["requests"]["1"]["generation"], 2)
        self.assertEqual(state["counters"]["abandonments"], 1)

    def test_clock_regression_tolerance(self) -> None:
        engine, state = make_engine(
            service_doc(),
            request_doc([request(1)]),
            config_doc(tolerance_ms=1),
        )
        engine.run_round(round_command("d0", 10))
        within = engine.run_round(round_command("within", 9))
        self.assertEqual(within["clock_ms"], 10)
        with self.assertRaises(GatewayError) as caught:
            engine.run_round(round_command("regress", 8))
        self.assertEqual(caught.exception.code, "E_CLOCK_REGRESSION")
        self.assertEqual(
            caught.exception.details["clock_watermark_ms"], 10
        )


if __name__ == "__main__":
    unittest.main()
