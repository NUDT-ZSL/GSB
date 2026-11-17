from __future__ import annotations

import json
import signal
import threading
import time
import unittest

from voice_dialog.cli import main as cli_main

from tests.helpers import TempCase, assert_stable_equal, run_once


class SnapshotTests(unittest.TestCase):
    def test_snapshot_version_mismatch_reports_business_code(self) -> None:
        case = TempCase()
        try:
            dataset = case.dataset("snapshot", segments=12, turns=6)
            config_path = case.write_config(case.config(scale_tier="small", concurrency=2, snapshot_interval=3))
            state = case.root / "state"
            first = run_once(dataset, config_path, state)
            snapshot_path = state / "snapshots" / "features.json"
            snapshot = json.loads(snapshot_path.read_text())
            snapshot["snapshot_version"] = 999
            snapshot_path.write_text(json.dumps(snapshot))
            second = run_once(dataset, config_path, state)
            errors = second["snapshots"]["errors"]
            self.assertTrue(any(item["code"] == "E_CONFIG_MISMATCH" for item in errors))
            self.assertTrue(any(item["stage"] == "features" for item in errors))
            assert_stable_equal(self, first, second)
        finally:
            case.close()

    def test_cli_sigint_commits_snapshot_and_exits_zero(self) -> None:
        case = TempCase()
        try:
            dataset = case.dataset("signal", segments=50, turns=12)
            config = case.config(concurrency=1, snapshot_interval=2, scale_tier="small")
            config_path = case.write_config(config)
            report_path = case.root / "interrupted.json"
            state = case.root / "state"

            def send_signal() -> None:
                time.sleep(0.08)
                try:
                    signal.raise_signal(signal.SIGINT)
                except Exception:
                    pass

            sender = threading.Thread(target=send_signal, daemon=True)
            sender.start()
            code = cli_main(
                [
                    "run",
                    "--samples",
                    str(dataset),
                    "--script",
                    str(dataset / "dialog_script.json"),
                    "--config",
                    str(config_path),
                    "--injections",
                    str(dataset / "anomalies.json"),
                    "--state",
                    str(state),
                    "--out",
                    str(report_path),
                    "--mode",
                    "incremental",
                ]
            )
            sender.join(timeout=2)
            self.assertEqual(code, 0)
            report = json.loads(report_path.read_text())
            self.assertEqual(report["status"], "interrupted")
            self.assertTrue((state / "snapshots" / "features.json").exists())
        finally:
            case.close()


if __name__ == "__main__":
    unittest.main()
