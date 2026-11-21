from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tests.helpers import SAMPLES, run_cli, tiny_config, tiny_manifest, write_json


class ValidationTests(unittest.TestCase):
    def test_duplicate_key_returns_business_code_and_no_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = tiny_manifest()
            manifest["objects"].append(dict(manifest["objects"][0]))
            write_json(root / "manifest.json", manifest)
            write_json(root / "config.json", tiny_config())
            database = root / "state.db"
            completed, payload = run_cli(
                "load",
                "--db",
                str(database),
                "--manifest",
                str(root / "manifest.json"),
                "--config",
                str(root / "config.json"),
                "--clock",
                "0",
            )
            self.assertEqual(completed.returncode, 2)
            self.assertEqual(
                payload["error"]["code"],
                "E_DUPLICATE_OBJECT_KEY",
            )
            self.assertFalse(database.exists())
            self.assertFalse(Path(f"{database}.lock").exists())

    def test_invalid_budget_returns_business_code_and_no_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_json(root / "manifest.json", tiny_manifest())
            config = tiny_config()
            config["round_byte_budget"] = 0
            write_json(root / "config.json", config)
            database = root / "state.db"
            completed, payload = run_cli(
                "load",
                "--db",
                str(database),
                "--manifest",
                str(root / "manifest.json"),
                "--config",
                str(root / "config.json"),
                "--clock",
                "0",
            )
            self.assertEqual(completed.returncode, 2)
            self.assertEqual(payload["error"]["code"], "E_INVALID_BUDGET")
            self.assertFalse(database.exists())

    def test_initial_capacity_overflow_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_json(root / "manifest.json", tiny_manifest())
            config = tiny_config()
            config["hot_capacity_bytes"] = 50
            write_json(root / "config.json", config)
            database = root / "state.db"
            completed, payload = run_cli(
                "load",
                "--db",
                str(database),
                "--manifest",
                str(root / "manifest.json"),
                "--config",
                str(root / "config.json"),
                "--clock",
                "0",
            )
            self.assertEqual(completed.returncode, 2)
            self.assertEqual(
                payload["error"]["code"],
                "E_INITIAL_CAPACITY_EXCEEDED",
            )
            self.assertFalse(database.exists())

    def test_clock_rollback_preserves_status_view(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "state.db"
            run_cli(
                "load",
                "--db",
                str(database),
                "--manifest",
                str(SAMPLES / "small" / "manifest.json"),
                "--config",
                str(SAMPLES / "small" / "config.json"),
                "--clock-table",
                str(SAMPLES / "small" / "clocks.json"),
                "--clock-step",
                "1",
                check=True,
            )
            run_cli(
                "migrate",
                "--db",
                str(database),
                "--bucket",
                "small-a",
                "--round",
                "1",
                "--clock-table",
                str(SAMPLES / "small" / "clocks.json"),
                "--clock-step",
                "2",
                check=True,
            )
            _, before = run_cli(
                "status",
                "--db",
                str(database),
                check=True,
            )
            completed, payload = run_cli(
                "migrate",
                "--db",
                str(database),
                "--bucket",
                "small-a",
                "--round",
                "2",
                "--clock-table",
                str(SAMPLES / "small" / "clocks.json"),
                "--clock-step",
                "6",
            )
            self.assertEqual(completed.returncode, 2)
            self.assertEqual(payload["error"]["code"], "E_CLOCK_ROLLBACK")
            _, after = run_cli(
                "status",
                "--db",
                str(database),
                check=True,
            )
            self.assertEqual(before, after)
            self.assertEqual(after["buckets"][0]["round_watermark"], 1)

    def test_clock_rollback_within_tolerance_keeps_watermark(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "state.db"
            run_cli(
                "load",
                "--db",
                str(database),
                "--manifest",
                str(SAMPLES / "small" / "manifest.json"),
                "--config",
                str(SAMPLES / "small" / "config.json"),
                "--clock",
                "1000",
                check=True,
            )
            result = run_cli(
                "migrate",
                "--db",
                str(database),
                "--bucket",
                "small-a",
                "--round",
                "1",
                "--clock",
                "1040",
                check=True,
            )[1]
            self.assertEqual(result["effective_clock"], 1040)
            result2 = run_cli(
                "migrate",
                "--db",
                str(database),
                "--bucket",
                "small-a",
                "--round",
                "2",
                "--clock",
                "1000",
                check=True,
            )[1]
            self.assertEqual(result2["effective_clock"], 1040)
            status = run_cli(
                "status",
                "--db",
                str(database),
                check=True,
            )[1]
            self.assertEqual(status["buckets"][0]["clock_watermark"], 1040)


if __name__ == "__main__":
    unittest.main()
