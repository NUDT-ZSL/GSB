from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from quota_ledger.utils import canonical_json

from .fixtures import PROJECT_ROOT, sample_limiter, sample_quotas


def _run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "quota_ledger", *args],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


class CliTests(unittest.TestCase):
    def test_all_required_subcommands(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "ledger.db"
            load = _run_cli(
                "load",
                "--db",
                str(db_path),
                "--quotas",
                str(PROJECT_ROOT / "samples" / "quotas.json"),
                "--limiter",
                str(PROJECT_ROOT / "samples" / "limiter.json"),
            )
            self.assertEqual(load.returncode, 0)
            self.assertTrue(json.loads(load.stdout)["created"])

            write = _run_cli(
                "write",
                "--db",
                str(db_path),
                "--events",
                str(PROJECT_ROOT / "samples" / "events.json"),
            )
            self.assertEqual(write.returncode, 0)
            results = [json.loads(line) for line in write.stdout.splitlines()]
            self.assertEqual(len(results), 14)
            self.assertEqual(results[3]["code"], "E_HARD_QUOTA")
            self.assertEqual(results[11]["code"], "E_RATE_LIMITED")

            delete_event = {
                "seq": 15,
                "tenant": "demo",
                "bucket": "media",
                "prefix": "alpha",
                "object_key": "a1",
                "bytes": 0,
                "deleted": True,
                "clock_ms": 800,
                "idempotency_key": "cli-delete-15",
            }
            delete = _run_cli(
                "delete",
                "--db",
                str(db_path),
                "--event-json",
                canonical_json(delete_event),
            )
            self.assertEqual(delete.returncode, 0)
            delete_result = json.loads(delete.stdout)
            self.assertEqual(delete_result["code"], "OK")
            self.assertEqual(delete_result["applied"]["bytes_delta"], -150)

            status = _run_cli("status", "--db", str(db_path))
            self.assertEqual(status.returncode, 0)
            status_value = json.loads(status.stdout)
            self.assertEqual(status_value["last_decided_seq"], 15)
            demo = next(
                layer
                for layer in status_value["layers"]
                if layer["level"] == "tenant" and layer["tenant"] == "demo"
            )
            self.assertEqual(demo["used_bytes"], 0)
            self.assertEqual(demo["used_objects"], 0)

            resume = _run_cli("resume", "--db", str(db_path))
            self.assertEqual(resume.returncode, 0)
            self.assertTrue(json.loads(resume.stdout)["verify"]["ok"])

            verify = _run_cli(
                "verify",
                "--db",
                str(db_path),
                "--source-events",
                str(PROJECT_ROOT / "samples" / "events.json"),
            )
            self.assertEqual(verify.returncode, 0)
            self.assertTrue(json.loads(verify.stdout)["ok"])

    def test_invalid_load_has_business_error_and_no_database(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bad_quota = dict(sample_quotas())
            bad_quota["tenants"] = []
            quota_path = Path(temp_dir) / "bad-quotas.json"
            quota_path.write_text(canonical_json(bad_quota), encoding="utf-8")
            limiter_path = Path(temp_dir) / "limiter.json"
            limiter_path.write_text(
                canonical_json(sample_limiter()),
                encoding="utf-8",
            )
            db_path = Path(temp_dir) / "ledger.db"
            result = _run_cli(
                "load",
                "--db",
                str(db_path),
                "--quotas",
                str(quota_path),
                "--limiter",
                str(limiter_path),
            )
            self.assertEqual(result.returncode, 2)
            self.assertEqual(json.loads(result.stdout)["code"], "E_BAD_HIERARCHY")
            self.assertFalse(db_path.exists())


if __name__ == "__main__":
    unittest.main()

