from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.helpers import SAMPLE_DIR, cli_json, run_cli


class CliDeterminismTests(unittest.TestCase):
    def test_two_fresh_runs_are_byte_identical(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            outputs: list[tuple[str, str, str, str, str]] = []
            for index in (1, 2):
                database = root / f"state-{index}.sqlite"
                load_result = run_cli(
                    "load",
                    "--bundle",
                    str(SAMPLE_DIR),
                    "--db",
                    str(database),
                    "--reset",
                    check=True,
                )
                schedule_result = run_cli(
                    "schedule",
                    "--bundle",
                    str(SAMPLE_DIR),
                    "--db",
                    str(database),
                    check=True,
                )
                status_result = run_cli(
                    "status",
                    "--bundle",
                    str(SAMPLE_DIR),
                    "--db",
                    str(database),
                    check=True,
                )
                verify_result = run_cli(
                    "verify",
                    "--bundle",
                    str(SAMPLE_DIR),
                    "--db",
                    str(database),
                    check=True,
                )
                inject_result = run_cli(
                    "inject",
                    "--bundle",
                    str(SAMPLE_DIR),
                    "--db",
                    str(database),
                    check=True,
                )
                self.assertEqual(load_result.returncode, 0)
                self.assertEqual(schedule_result.returncode, 0)
                self.assertEqual(status_result.returncode, 0)
                self.assertEqual(verify_result.returncode, 0)
                outputs.append(
                    (
                        load_result.stdout,
                        schedule_result.stdout,
                        status_result.stdout,
                        verify_result.stdout,
                        inject_result.stdout,
                    )
                )
            self.assertEqual(outputs[0], outputs[1])

    def test_report_output_matches_stdout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "state.sqlite"
            report = root / "reports" / "status.json"
            run_cli(
                "load",
                "--bundle",
                str(SAMPLE_DIR),
                "--db",
                str(database),
                check=True,
            )
            result = run_cli(
                "status",
                "--bundle",
                str(SAMPLE_DIR),
                "--db",
                str(database),
                "--out",
                str(report),
                check=True,
            )
            self.assertEqual(report.read_text(encoding="utf-8"), result.stdout)

    def test_inject_command_filters_an_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "state.sqlite"
            run_cli(
                "load",
                "--bundle",
                str(SAMPLE_DIR),
                "--db",
                str(database),
                check=True,
            )
            result = cli_json(
                "inject",
                "--bundle",
                str(SAMPLE_DIR),
                "--db",
                str(database),
                "--id",
                "inj-weight-r3",
            )
            self.assertTrue(result["ok"])
            self.assertEqual(len(result["injections"]), 1)
            self.assertEqual(result["injections"][0]["id"], "inj-weight-r3")
            self.assertFalse(result["injections"][0]["consumed"])

    def test_replay_command_returns_validated_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "state.sqlite"
            run_cli(
                "load",
                "--bundle",
                str(SAMPLE_DIR),
                "--db",
                str(database),
                check=True,
            )
            run_cli(
                "schedule",
                "--bundle",
                str(SAMPLE_DIR),
                "--db",
                str(database),
                check=True,
            )
            result = cli_json(
                "replay",
                "--db",
                str(database),
            )
            self.assertTrue(result["ok"])
            self.assertEqual(result["replay"]["ledger_version"], 8)
            self.assertEqual(len(result["ledger"]["entries"]), 8)


if __name__ == "__main__":
    unittest.main()
