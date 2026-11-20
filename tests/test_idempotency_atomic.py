from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from quota_ledger import LedgerError, QuotaLedger
from quota_ledger.utils import canonical_json

from .fixtures import sample_event, sample_limiter, sample_quotas


class IdempotencyAndAtomicityTests(unittest.TestCase):
    def test_exact_replay_and_independent_conflict_codes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = QuotaLedger.load(
                Path(temp_dir) / "ledger.db",
                sample_quotas(),
                sample_limiter(),
            )
            event = sample_event(1)
            first = ledger.write(event)
            status_after_first = canonical_json(ledger.status())
            replay = ledger.write(event)
            self.assertEqual(canonical_json(first), canonical_json(replay))
            self.assertEqual(status_after_first, canonical_json(ledger.status()))

            changed = dict(event)
            changed["seq"] = 2
            changed["bytes"] = 151
            with self.assertRaises(LedgerError) as context:
                ledger.write(changed)
            self.assertEqual(
                context.exception.code,
                "E_IDEMPOTENCY_CONFLICT",
            )
            self.assertEqual(status_after_first, canonical_json(ledger.status()))

            seq_conflict = dict(event)
            seq_conflict["idempotency_key"] = "different-1"
            with self.assertRaises(LedgerError) as context:
                ledger.write(seq_conflict)
            self.assertEqual(context.exception.code, "E_EVENT_SEQ_CONFLICT")
            self.assertEqual(status_after_first, canonical_json(ledger.status()))

    def test_denied_rate_decision_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = QuotaLedger.load(
                Path(temp_dir) / "ledger.db",
                sample_quotas(),
                sample_limiter(),
            )
            first = {
                "seq": 1,
                "tenant": "rate",
                "bucket": "media",
                "prefix": "p",
                "object_key": "a",
                "bytes": 1,
                "deleted": False,
                "clock_ms": 1000,
                "idempotency_key": "a-1",
            }
            second = {
                "seq": 2,
                "tenant": "rate",
                "bucket": "media",
                "prefix": "p",
                "object_key": "b",
                "bytes": 1,
                "deleted": False,
                "clock_ms": 1000,
                "idempotency_key": "b-2",
            }
            third = dict(
                second,
                seq=3,
                object_key="c",
                idempotency_key="c-3",
            )
            fourth = dict(
                second,
                seq=4,
                object_key="d",
                idempotency_key="d-4",
            )
            ledger.write(first)
            ledger.write(second)
            ledger.write(third)
            denied = ledger.write(fourth)
            self.assertEqual(denied["code"], "E_RATE_LIMITED")
            status_after_denial = canonical_json(ledger.status())
            repeated = ledger.write(fourth)
            self.assertEqual(canonical_json(denied), canonical_json(repeated))
            self.assertEqual(status_after_denial, canonical_json(ledger.status()))

    def test_injected_failure_rolls_back_token_quota_and_decision(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = QuotaLedger.load(
                Path(temp_dir) / "ledger.db",
                sample_quotas(),
                sample_limiter(),
            )
            ledger.write(sample_event(1))
            before = canonical_json(ledger.status())
            event = sample_event(2)
            with self.assertRaises(LedgerError) as context:
                ledger.write(event, fault_stage="after_ledger")
            self.assertEqual(context.exception.code, "E_INJECTED_FAILURE")
            self.assertEqual(before, canonical_json(ledger.status()))

            result = ledger.write(event)
            self.assertEqual(result["code"], "OK")
            self.assertTrue(ledger.verify()["ok"])


if __name__ == "__main__":
    unittest.main()

