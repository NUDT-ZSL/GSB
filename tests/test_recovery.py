from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from quota_ledger import QuotaLedger
from quota_ledger.utils import canonical_json

from .fixtures import PROJECT_ROOT, sample_event, sample_limiter, sample_quotas


def _crash_write(
    db_path: Path,
    event: dict,
    stage: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "quota_ledger",
            "write",
            "--db",
            str(db_path),
            "--event-json",
            canonical_json(event),
            "--crash-stage",
            stage,
        ],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


class RecoveryTests(unittest.TestCase):
    def test_forced_termination_before_commit_and_orphan_intent_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "ledger.db"
            ledger = QuotaLedger.load(db_path, sample_quotas(), sample_limiter())
            ledger.write(sample_event(1))
            before = canonical_json(ledger.status())

            crashed = _crash_write(
                db_path,
                sample_event(2),
                "before_commit",
            )
            self.assertEqual(crashed.returncode, 73)
            self.assertEqual(before, canonical_json(ledger.status()))

            resumed = ledger.resume()
            self.assertTrue(resumed["ok"])
            self.assertEqual(resumed["cleaned_count"], 0)
            self.assertTrue(resumed["verify"]["ok"])

            accepted = ledger.write(sample_event(2))
            self.assertEqual(accepted["code"], "OK")
            status_after_commit = canonical_json(ledger.status())

            orphan = _crash_write(
                db_path,
                sample_event(3),
                "after_intent",
            )
            self.assertEqual(orphan.returncode, 74)
            status_with_orphan = ledger.status()
            self.assertEqual(status_with_orphan["counts"]["pending_operations"], 1)
            self.assertEqual(status_with_orphan["last_decided_seq"], 2)

            cleaned = ledger.resume()
            self.assertTrue(cleaned["ok"])
            self.assertEqual(cleaned["cleaned_count"], 1)
            self.assertTrue(cleaned["verify"]["ok"])
            self.assertEqual(ledger.status()["counts"]["pending_operations"], 0)
            self.assertEqual(status_after_commit, canonical_json(ledger.status()))
            self.assertTrue(ledger.verify([sample_event(1), sample_event(2)])["ok"])

    def test_verify_detects_tampering_without_auto_repair(self) -> None:
        import sqlite3

        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "ledger.db"
            ledger = QuotaLedger.load(db_path, sample_quotas(), sample_limiter())
            ledger.write(sample_event(1))
            connection = sqlite3.connect(db_path)
            try:
                connection.execute(
                    """
                    UPDATE usage SET used_bytes = used_bytes - 1
                    WHERE path = (
                        SELECT path FROM quota_layers
                        WHERE level = 'prefix' AND tenant = 'demo'
                        ORDER BY path LIMIT 1
                    )
                    """
                )
                connection.commit()
            finally:
                connection.close()
            verification = ledger.verify()
            self.assertFalse(verification["ok"])
            self.assertTrue(any(
                mismatch["field"].startswith("usage[")
                for mismatch in verification["mismatches"]
            ))


if __name__ == "__main__":
    unittest.main()

