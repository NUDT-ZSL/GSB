from __future__ import annotations

import unittest

from qos_pipeline.engine import Engine
from qos_pipeline.fixed import bytes_to_units
from qos_pipeline.sample import load_sample
from qos_pipeline.serialization import canonical_json, digest_value

from .helpers import ROOT


class EngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sample = load_sample(ROOT / "samples" / "manifest.json")
        cls.engine = Engine(cls.sample)
        cls.payloads, cls.final_state = cls.engine.replay()

    def test_replay_is_bit_for_bit_deterministic(self) -> None:
        payloads, state = Engine(self.sample).replay()
        self.assertEqual(
            canonical_json(self.payloads), canonical_json(payloads)
        )
        self.assertEqual(
            digest_value(self.final_state), digest_value(state)
        )

    def test_each_event_has_exactly_one_final_disposition(self) -> None:
        passed = [
            item["seq"] for payload in self.payloads for item in payload["passes"]
        ]
        dropped = [
            item["seq"] for payload in self.payloads for item in payload["drops"]
        ]
        waiting = [
            item["seq"] for item in self.payloads[-1]["waiting"]
        ]
        all_sequences = passed + dropped + waiting
        self.assertEqual(len(all_sequences), len(set(all_sequences)))
        self.assertEqual(set(all_sequences), {item.seq for item in self.sample.events})

    def test_queue_and_token_bounds_hold_at_every_step(self) -> None:
        class_map = self.sample.class_map
        for payload in self.payloads:
            for name, record in payload["classes"].items():
                config = class_map[name]
                self.assertLessEqual(
                    record["queue"]["occupancy_bytes"], config.queue_depth_bytes
                )
                self.assertGreaterEqual(
                    record["tokens"]["committed_units"], 0
                )
                self.assertGreaterEqual(record["tokens"]["peak_units"], 0)
                self.assertLessEqual(
                    record["tokens"]["committed_units"],
                    bytes_to_units(config.committed_burst_bytes),
                )
                self.assertLessEqual(
                    record["tokens"]["peak_units"],
                    bytes_to_units(config.peak_burst_bytes),
                )

    def test_parent_send_never_exceeds_post_shaping_budget(self) -> None:
        for payload in self.payloads:
            for report in payload["scheduler"].values():
                sent = int(round(float(report["sent_bytes"]) * 1000))
                available = int(round(float(report["available_bytes"]) * 1000))
                self.assertLessEqual(sent, available)

    def test_fifo_order_is_preserved_per_class(self) -> None:
        for class_name in ("interactive", "bulk", "telemetry"):
            passes = [
                item
                for payload in self.payloads
                for item in payload["passes"]
                if item["class"] == class_name
            ]
            expected = sorted(
                passes, key=lambda item: (item["arrival_ms"], item["seq"])
            )
            self.assertEqual(
                [item["seq"] for item in passes],
                [item["seq"] for item in expected],
            )
            self.assertTrue(all(item["wait_ms"] >= 0 for item in passes))

    def test_wred_and_tail_drops_are_traceable(self) -> None:
        drops = [item for payload in self.payloads for item in payload["drops"]]
        bases = {item["basis"] for item in drops}
        self.assertIn("tail_queue_full", bases)
        self.assertTrue(
            any(basis.startswith("wred_") for basis in bases),
            "sample must exercise WRED decisions",
        )
        for item in drops:
            if item["class"] == "bulk":
                self.assertIn("wred_probability_ppm", item)
                self.assertTrue(item["drop_candidate"])
                if item["basis"] == "wred_forced_queue_overflow":
                    self.assertIsNone(item["wred_random_ppm"])
                else:
                    self.assertIsNotNone(item["wred_random_ppm"])

    def test_totals_account_for_all_arrival_bytes(self) -> None:
        arrival_bytes = sum(item.bytes for item in self.sample.events)
        final = self.payloads[-1]
        passed = sum(
            event["bytes"] for payload in self.payloads for event in payload["passes"]
        )
        dropped = sum(
            event["bytes"] for payload in self.payloads for event in payload["drops"]
        )
        waiting = final["totals"]["waiting_bytes"]
        self.assertEqual(passed + dropped + waiting, arrival_bytes)


if __name__ == "__main__":
    unittest.main()

