from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.helpers import run_cli, tiny_config, tiny_manifest, write_json


class MigrationStateTests(unittest.TestCase):
    def _load_tiny(self, root: Path) -> Path:
        manifest = write_json(root / "manifest.json", tiny_manifest())
        config = write_json(root / "config.json", tiny_config())
        database = root / "state.db"
        run_cli(
            "load",
            "--db",
            str(database),
            "--manifest",
            str(manifest),
            "--config",
            str(config),
            "--clock",
            "0",
            check=True,
        )
        return database

    def test_failure_backoff_retry_limit_and_idempotence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            database = self._load_tiny(root)
            first = run_cli(
                "migrate",
                "--db",
                str(database),
                "--bucket",
                "tiny-a",
                "--round",
                "1",
                "--clock",
                "0",
                "--fail-key",
                "cold-high",
                check=True,
            )[1]
            self.assertEqual(first["failed_count"], 1)
            replay = run_cli(
                "migrate",
                "--db",
                str(database),
                "--bucket",
                "tiny-a",
                "--round",
                "1",
                "--clock",
                "0",
                "--fail-key",
                "cold-high",
                check=True,
            )[1]
            self.assertEqual(first, replay)
            conflict = run_cli(
                "migrate",
                "--db",
                str(database),
                "--bucket",
                "tiny-a",
                "--round",
                "1",
                "--clock",
                "0",
                "--fail-count",
                "1",
            )
            self.assertEqual(conflict[0].returncode, 2)
            self.assertEqual(
                conflict[1]["error"]["code"],
                "E_ROUND_CONFLICT",
            )
            status = run_cli(
                "status",
                "--db",
                str(database),
                check=True,
            )[1]["buckets"][0]
            cold_high = next(
                obj
                for obj in status["objects"]
                if obj["key"] == "cold-high"
            )
            self.assertEqual(cold_high["migration_state"], "failed")
            self.assertEqual(cold_high["failure_count"], 1)
            self.assertEqual(cold_high["backoff_until_clock"], 50)

            run_cli(
                "migrate",
                "--db",
                str(database),
                "--bucket",
                "tiny-a",
                "--round",
                "2",
                "--clock",
                "10",
                check=True,
            )
            status = run_cli(
                "status",
                "--db",
                str(database),
                check=True,
            )[1]["buckets"][0]
            cold_high = next(
                obj
                for obj in status["objects"]
                if obj["key"] == "cold-high"
            )
            self.assertEqual(cold_high["failure_count"], 1)

            run_cli(
                "migrate",
                "--db",
                str(database),
                "--bucket",
                "tiny-a",
                "--round",
                "3",
                "--clock",
                "50",
                "--fail-key",
                "cold-high",
                check=True,
            )
            run_cli(
                "migrate",
                "--db",
                str(database),
                "--bucket",
                "tiny-a",
                "--round",
                "4",
                "--clock",
                "100",
                check=True,
            )
            final = run_cli(
                "migrate",
                "--db",
                str(database),
                "--bucket",
                "tiny-a",
                "--round",
                "5",
                "--clock",
                "150",
                "--fail-key",
                "cold-high",
                check=True,
            )[1]
            self.assertEqual(final["failed_count"], 0)
            self.assertIn(
                ("abandoned", "abandoned_retry_limit"),
                {(item["state"], item["outcome"]) for item in final["outcomes"]},
            )
            status = run_cli(
                "status",
                "--db",
                str(database),
                check=True,
            )[1]["buckets"][0]
            cold_high = next(
                obj
                for obj in status["objects"]
                if obj["key"] == "cold-high"
            )
            self.assertEqual(cold_high["migration_state"], "abandoned")
            self.assertEqual(
                cold_high["last_outcome"],
                "abandoned_retry_limit",
            )
            self.assertEqual(cold_high["failure_count"], 3)
            self.assertEqual(cold_high["backoff_until_clock"], 350)
            verify = run_cli(
                "verify",
                "--db",
                str(database),
                check=True,
            )[1]
            self.assertTrue(verify["ok"])

    def test_verify_detects_ledger_corruption(self) -> None:
        import sqlite3

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            database = self._load_tiny(root)
            run_cli(
                "migrate",
                "--db",
                str(database),
                "--bucket",
                "tiny-a",
                "--round",
                "1",
                "--clock",
                "0",
                check=True,
            )
            with sqlite3.connect(database) as connection:
                connection.execute(
                    """
                    UPDATE layers SET used_bytes = used_bytes + 1
                    WHERE bucket = 'tiny-a' AND layer = 'hot'
                    """
                )
            completed, payload = run_cli(
                "verify",
                "--db",
                str(database),
            )
            self.assertEqual(completed.returncode, 3)
            self.assertFalse(payload["ok"])
            codes = {item["code"] for item in payload["violations"]}
            self.assertIn("E_LEDGER_MISMATCH", codes)


if __name__ == "__main__":
    unittest.main()
