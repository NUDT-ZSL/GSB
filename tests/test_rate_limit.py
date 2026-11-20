from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from quota_ledger import LedgerError, QuotaLedger
from quota_ledger.utils import canonical_json
from quota_ledger.validation import parse_event_document

from .fixtures import sample_events, sample_limiter, sample_quotas


class RateLimitTests(unittest.TestCase):
    def test_token_bucket_rejection_wait_refill_and_rollback(self) -> None:
        events = parse_event_document(sample_events()[8:13], None)
        decisions: list[dict] = []
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "ledger.db"
            ledger = QuotaLedger.load(db_path, sample_quotas(), sample_limiter())
            for event in events:
                decisions.append(ledger.write(event))

            self.assertEqual(
                [decision["code"] for decision in decisions],
                ["OK", "OK", "OK", "E_RATE_LIMITED", "OK"],
            )
            denied = decisions[3]
            self.assertEqual(denied["first_violation"]["kind"], "rate_limit")
            self.assertEqual(denied["first_violation"]["wait_ms"], 997)
            self.assertEqual(denied["first_violation"]["retry_after_ms"], 997)
            self.assertEqual(
                denied["basis"]["rate_limit"]["tokens_after_refill_micro"],
                3000,
            )

            status = ledger.status()
            rate_token = next(
                token for token in status["tokens"] if token["tenant"] == "rate"
            )
            self.assertEqual(rate_token["tokens_micro"], 4000)
            self.assertEqual(rate_token["last_refill_ms"], 2004)

            before_rollback = canonical_json(ledger.status())
            rollback_event = {
                "seq": 14,
                "tenant": "rate",
                "bucket": "media",
                "prefix": "p",
                "object_key": "backwards",
                "bytes": 1,
                "deleted": False,
                "clock_ms": 1800,
                "idempotency_key": "backwards-14",
            }
            with self.assertRaises(LedgerError) as context:
                ledger.write(rollback_event)
            self.assertEqual(context.exception.code, "E_CLOCK_ROLLBACK")
            self.assertEqual(before_rollback, canonical_json(ledger.status()))
            self.assertTrue(ledger.verify(events)["ok"])

    def test_same_event_sequence_is_byte_deterministic(self) -> None:
        events = parse_event_document(sample_events()[8:13], None)
        outputs = []
        for _ in range(2):
            with tempfile.TemporaryDirectory() as temp_dir:
                ledger = QuotaLedger.load(
                    Path(temp_dir) / "ledger.db",
                    sample_quotas(),
                    sample_limiter(),
                )
                outputs.append(
                    canonical_json([ledger.write(event) for event in events])
                )
        self.assertEqual(outputs[0], outputs[1])


if __name__ == "__main__":
    unittest.main()

