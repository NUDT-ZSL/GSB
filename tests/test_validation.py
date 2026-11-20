from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from quota_ledger import QuotaLedger
from quota_ledger.errors import (
    E_BAD_EVENT,
    E_BAD_HIERARCHY,
    E_BAD_LIMITER,
    E_BAD_QUOTA,
    LedgerError,
)
from quota_ledger.ledger import load_configuration
from quota_ledger.validation import parse_event_document

from .fixtures import sample_limiter, sample_quotas


class ValidationTests(unittest.TestCase):
    def test_invalid_quota_documents_do_not_create_database(self) -> None:
        quota = sample_quotas()
        limiter = sample_limiter()
        duplicate = sample_quotas()
        duplicate["tenants"].append(duplicate["tenants"][0])
        bad_quota = sample_quotas()
        bad_quota["tenants"][0]["soft_pct"] = 99
        bad_quota["tenants"][0]["hard_pct"] = 50

        cases = (
            (duplicate, limiter, E_BAD_HIERARCHY),
            (bad_quota, limiter, E_BAD_QUOTA),
        )
        for quota_document, limiter_document, code in cases:
            with self.subTest(code=code), tempfile.TemporaryDirectory() as temp_dir:
                db_path = Path(temp_dir) / "ledger.db"
                with self.assertRaises(LedgerError) as context:
                    load_configuration(db_path, quota_document, limiter_document)
                self.assertEqual(context.exception.code, code)
                self.assertFalse(db_path.exists())

    def test_invalid_limiter_does_not_create_database(self) -> None:
        limiter = sample_limiter()
        limiter["tenants"][0]["refill_rate_per_second"] = 0
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "ledger.db"
            with self.assertRaises(LedgerError) as context:
                load_configuration(db_path, sample_quotas(), limiter)
            self.assertEqual(context.exception.code, E_BAD_LIMITER)
            self.assertFalse(db_path.exists())

    def test_non_increasing_event_stream_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "ledger.db"
            ledger = QuotaLedger.load(db_path, sample_quotas(), sample_limiter())
            event_2 = {
                "seq": 2,
                "tenant": "demo",
                "bucket": "media",
                "prefix": "alpha",
                "object_key": "x",
                "bytes": 1,
                "deleted": False,
                "clock_ms": 10,
                "idempotency_key": "x-2",
            }
            event_1 = dict(event_2, seq=1, idempotency_key="x-1")
            with self.assertRaises(LedgerError) as context:
                parse_event_document([event_2, event_1], ledger.quota)
            self.assertEqual(context.exception.code, E_BAD_EVENT)
            status = ledger.status()
            self.assertEqual(status["counts"]["events"], 0)
            self.assertEqual(status["last_decided_seq"], 0)

    def test_unknown_event_hierarchy_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "ledger.db"
            ledger = QuotaLedger.load(db_path, sample_quotas(), sample_limiter())
            event = {
                "seq": 1,
                "tenant": "missing",
                "bucket": "media",
                "prefix": "alpha",
                "object_key": "x",
                "bytes": 1,
                "deleted": False,
                "clock_ms": 0,
                "idempotency_key": "unknown-1",
            }
            with self.assertRaises(LedgerError) as context:
                ledger.write(event)
            self.assertEqual(context.exception.code, E_BAD_HIERARCHY)
            self.assertEqual(ledger.status()["counts"]["events"], 0)

    def test_invalid_batch_is_fully_prevalidated_before_any_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "ledger.db"
            ledger = QuotaLedger.load(db_path, sample_quotas(), sample_limiter())
            valid = {
                "seq": 1,
                "tenant": "demo",
                "bucket": "media",
                "prefix": "alpha",
                "object_key": "x",
                "bytes": 1,
                "deleted": False,
                "clock_ms": 0,
                "idempotency_key": "batch-1",
            }
            invalid = dict(
                valid,
                seq=2,
                tenant="missing",
                idempotency_key="batch-2",
            )
            with self.assertRaises(LedgerError):
                ledger.write_batch([valid, invalid])
            self.assertEqual(ledger.status()["counts"]["events"], 0)

    def test_load_same_configuration_is_idempotent_and_conflicts_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "ledger.db"
            first = load_configuration(db_path, sample_quotas(), sample_limiter())
            second = load_configuration(db_path, sample_quotas(), sample_limiter())
            self.assertTrue(first["created"])
            self.assertFalse(second["created"])

            changed = sample_quotas()
            changed["tenants"][0]["max_bytes"] = 999
            with self.assertRaises(LedgerError) as context:
                load_configuration(db_path, changed, sample_limiter())
            self.assertEqual(context.exception.code, "E_CONFIG_CONFLICT")


if __name__ == "__main__":
    unittest.main()

