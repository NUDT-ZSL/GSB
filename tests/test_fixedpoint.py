from __future__ import annotations

import unittest

from gateway.canonical import canonical_json
from gateway.engine import Engine, build_genesis
from gateway.fixedpoint import (
    charge_token_ledger,
    new_token_ledger,
    refill_token_ledger,
    token_view,
)
from gateway.validation import normalize_inputs
from tests.helpers import (
    config_doc,
    make_inputs,
    request,
    request_doc,
    service_doc,
)


class FixedPointTests(unittest.TestCase):
    def test_sub_unit_remainder_is_carried(self) -> None:
        ledger = new_token_ledger(
            capacity_tokens=1, rate_per_second=1, scale=7, clock_ms=0
        )
        charge_token_ledger(ledger, 7)
        self.assertEqual(ledger["balance_fixed"], 0)

        first = refill_token_ledger(ledger, 1)
        self.assertEqual(first["added_fixed"], 0)
        self.assertEqual(first["remainder"], 7)
        self.assertEqual(ledger["balance_fixed"], 0)
        self.assertEqual(ledger["last_refill_fraction"], 7)

        second = refill_token_ledger(ledger, 143)
        self.assertEqual(second["added_fixed"], 1)
        self.assertEqual(second["remainder"], 1)
        self.assertEqual(ledger["balance_fixed"], 1)

        third = refill_token_ledger(ledger, 144)
        self.assertEqual(third["added_fixed"], 0)
        self.assertEqual(third["remainder"], 8)
        self.assertEqual(ledger["balance_fixed"], 1)

        fourth = refill_token_ledger(ledger, 286)
        self.assertEqual(fourth["added_fixed"], 1)
        self.assertEqual(fourth["remainder"], 2)
        self.assertEqual(ledger["balance_fixed"], 2)

    def test_capacity_clamp_and_overflow_are_audited(self) -> None:
        ledger = new_token_ledger(
            capacity_tokens=2, rate_per_second=10, scale=7, clock_ms=0
        )
        charge_token_ledger(ledger, 14)
        refill_token_ledger(ledger, 1000)
        self.assertEqual(ledger["balance_fixed"], 14)
        self.assertGreater(ledger["overflow_discarded_fixed"], 0)
        view = token_view(ledger)
        self.assertEqual(view["rounding_direction"], "floor")
        self.assertEqual(view["refill_denominator"], 1000)

    def test_identical_commands_produce_identical_state(self) -> None:
        services = service_doc()
        requests = request_doc([request(1), request(2, priority=2)])
        config = config_doc(scale=7, capacity=10, rate=1)
        normalized = make_inputs(services, requests, config)
        state_a = build_genesis(normalized)
        state_b = build_genesis(normalized)
        engine_a = Engine(state_a)
        engine_b = Engine(state_b)
        commands = [
            {
                "round_id": "a",
                "kind": "dispatch",
                "clock_ms": 1,
                "budget_ms": 10,
            },
            {
                "round_id": "b",
                "kind": "dispatch",
                "clock_ms": 100,
                "budget_ms": 10,
                "outcomes": [
                    {
                        "request_id": 2,
                        "generation": 1,
                        "replica_id": "r1",
                        "outcome": "success",
                    }
                ],
            },
        ]
        for command in commands:
            result_a = engine_a.run_round(command)
            result_b = engine_b.run_round(command)
            self.assertEqual(canonical_json(result_a), canonical_json(result_b))
        self.assertEqual(canonical_json(state_a), canonical_json(state_b))
        self.assertNotEqual(
            state_a["global_token"]["refill_remainder"],
            -1,
        )


if __name__ == "__main__":
    unittest.main()
