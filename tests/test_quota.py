from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from quota_ledger import QuotaLedger
from quota_ledger.validation import parse_event_document

from .fixtures import sample_events, sample_limiter, sample_quotas


class HierarchicalQuotaTests(unittest.TestCase):
    def test_soft_hard_delete_and_replay_truth(self) -> None:
        events = parse_event_document(
            sample_events()[:8],
            None,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "ledger.db"
            ledger = QuotaLedger.load(db_path, sample_quotas(), sample_limiter())
            results = [ledger.write(event) for event in events]

            self.assertEqual(
                [result["code"] for result in results],
                ["OK", "OK", "OK", "E_HARD_QUOTA", "OK", "OK", "OK", "OK"],
            )
            hard = results[3]
            self.assertEqual(hard["first_violation"]["level"], "prefix")
            self.assertEqual(hard["first_violation"]["metric"], "bytes")
            self.assertEqual(hard["first_violation"]["projected"], 450)
            self.assertEqual(hard["first_violation"]["limit"], 400)

            self.assertEqual(results[1]["soft_transitions"][0]["transition"], "ENTER")
            self.assertEqual(results[2]["soft_transitions"][0]["level"], "bucket")
            self.assertEqual(results[5]["soft_transitions"][0]["transition"], "EXIT")
            self.assertEqual(results[6]["soft_transitions"][0]["level"], "prefix")

            status = ledger.status()
            prefix = next(
                layer
                for layer in status["layers"]
                if layer["level"] == "prefix" and layer["prefix"] == "alpha"
            )
            bucket = next(
                layer
                for layer in status["layers"]
                if layer["level"] == "bucket" and layer["tenant"] == "demo"
            )
            tenant = next(
                layer
                for layer in status["layers"]
                if layer["level"] == "tenant" and layer["tenant"] == "demo"
            )
            self.assertEqual(prefix["used_bytes"], 150)
            self.assertEqual(prefix["used_objects"], 1)
            self.assertEqual(bucket["used_bytes"], 150)
            self.assertEqual(tenant["used_bytes"], 150)
            self.assertFalse(prefix["soft_active"])
            self.assertEqual(status["counts"]["soft_alerts"], 4)
            self.assertEqual(status["counts"]["rejected"], 1)

            self.assertTrue(ledger.verify(events)["ok"])
            self.assertEqual(status["counts"]["objects"], 1)

    def test_delete_returns_persisted_size_not_event_size(self) -> None:
        events = parse_event_document(sample_events()[:6], None)
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "ledger.db"
            ledger = QuotaLedger.load(db_path, sample_quotas(), sample_limiter())
            for event in events:
                ledger.write(event)
            delete_result = ledger.verify(events)
            self.assertTrue(delete_result["ok"])
            status = ledger.status()
            prefix = next(
                layer
                for layer in status["layers"]
                if layer["level"] == "prefix" and layer["prefix"] == "alpha"
            )
            self.assertEqual(prefix["used_bytes"], 300)
            self.assertEqual(prefix["used_objects"], 3)

    def test_conservation_holds_after_interleaved_writes_and_deletes(self) -> None:
        events = parse_event_document(sample_events()[:8], None)
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "ledger.db"
            ledger = QuotaLedger.load(db_path, sample_quotas(), sample_limiter())
            for event in events:
                ledger.write(event)
            verification = ledger.verify(events)
            self.assertTrue(verification["checks"]["conservation_holds"])
            self.assertEqual(verification["mismatch_count"], 0)


if __name__ == "__main__":
    unittest.main()

