from __future__ import annotations

import json
import signal
import tempfile
import unittest
from pathlib import Path

from tests.support import ROOT, cli


class RecoveryTests(unittest.TestCase):
    def test_sigkill_at_every_checkpoint_resumes_bitwise_identically(self) -> None:
        phases = (
            "window_open",
            "batch_adjudication",
            "batch_commit",
            "window_commit",
            "batch_execute",
        )
        for phase in phases:
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as temp:
                temp_path = Path(temp)
                db_path = temp_path / f"{phase}.db"
                loaded = cli(
                    "load",
                    str(ROOT / "samples" / "default"),
                    "--db",
                    str(db_path),
                )
                self.assertEqual(loaded.returncode, 0, loaded.stderr)
                injected = cli(
                    "inject",
                    "--db",
                    str(db_path),
                    "--window-index",
                    "0",
                    "--phase",
                    phase,
                    "--batch-index",
                    "0",
                )
                self.assertEqual(injected.returncode, 0, injected.stderr)
                killed = cli("run", "--db", str(db_path))
                self.assertEqual(
                    killed.returncode,
                    -signal.SIGKILL,
                    f"expected real SIGKILL, got {killed.returncode}: {killed.stderr}",
                )
                partial = json.loads(
                    cli("status", "--db", str(db_path)).stdout
                )
                self.assertFalse(partial["complete"])
                self.assertLess(
                    partial["logical_clock_ms"], partial["target_clock_ms"]
                )
                self.assertTrue(
                    any(item["fired"] for item in partial["injections"])
                )
                resumed = cli("resume", "--db", str(db_path))
                self.assertEqual(resumed.returncode, 0, resumed.stderr)
                self.assertTrue(json.loads(resumed.stdout)["complete"])
                verified = cli("verify", "--db", str(db_path))
                self.assertEqual(
                    verified.returncode,
                    0,
                    verified.stdout + verified.stderr,
                )
                payload = json.loads(verified.stdout)
                self.assertTrue(payload["bitwise_match"])
                self.assertEqual(payload["difference_count"], 0)
                self.assertTrue(payload["internal_checks"]["passed"])

    def test_half_committed_window_keeps_existing_batch_and_replays_gap(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            db_path = Path(temp) / "half.db"
            self.assertEqual(
                cli(
                    "load",
                    str(ROOT / "samples" / "default"),
                    "--db",
                    str(db_path),
                ).returncode,
                0,
            )
            self.assertEqual(
                cli(
                    "inject",
                    "--db",
                    str(db_path),
                    "--window-index",
                    "0",
                    "--phase",
                    "batch_commit",
                    "--batch-index",
                    "0",
                ).returncode,
                0,
            )
            killed = cli("run", "--db", str(db_path))
            self.assertEqual(killed.returncode, -signal.SIGKILL)
            partial = json.loads(cli("status", "--db", str(db_path)).stdout)
            open_windows = [
                window for window in partial["windows"] if window["status"] == "OPEN"
            ]
            self.assertEqual(len(open_windows), 1)
            self.assertGreaterEqual(len(open_windows[0]["batches"]), 1)
            before_ids = [
                batch["batch_key"] for batch in open_windows[0]["batches"]
            ]
            self.assertEqual(
                cli("resume", "--db", str(db_path)).returncode,
                0,
            )
            after = json.loads(cli("status", "--db", str(db_path)).stdout)
            final_batches = [
                batch
                for window in after["windows"]
                for batch in window["batches"]
                if window["replica_id"] == open_windows[0]["replica_id"]
                and window["window_index"] == open_windows[0]["window_index"]
            ]
            final_keys = [batch["batch_key"] for batch in final_batches]
            self.assertTrue(set(before_ids) <= set(final_keys))
            self.assertEqual(cli("verify", "--db", str(db_path)).returncode, 0)


if __name__ == "__main__":
    unittest.main()
