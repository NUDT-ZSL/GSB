from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run_cli(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "livingroom_stereo", *arguments],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


class CliTests(unittest.TestCase):
    def test_launcher_script_is_self_contained(self) -> None:
        result = subprocess.run(
            [str(ROOT / "bin" / "lsr3d"), "validate", "examples/living_room.json"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["valid"])

    def test_validate_and_evaluate(self) -> None:
        validation = run_cli(
            "validate", "examples/living_room.json"
        )
        self.assertEqual(validation.returncode, 0, validation.stderr)
        self.assertTrue(json.loads(validation.stdout)["valid"])

        evaluation = run_cli(
            "evaluate", "examples/living_room.json"
        )
        self.assertEqual(evaluation.returncode, 0, evaluation.stderr)
        payload = json.loads(evaluation.stdout)
        self.assertEqual(payload["project_id"], "living-room-stereo-demo")
        self.assertIn("poses", payload)
        self.assertIn("alignment", payload)

    def test_incremental_cli_with_state_and_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            state = temporary / "changed.state.json"
            incremental_output = temporary / "incremental.json"
            result = run_cli(
                "incremental",
                "examples/living_room.json",
                "examples/living_room.adjusted.json",
                "--write-state",
                str(state),
                "--output",
                str(incremental_output),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(
                incremental_output.read_text(encoding="utf-8")
            )
            self.assertTrue(payload["consistency"]["consistent"])
            self.assertIsNone(
                payload["consistency"]["first_divergence"]
            )
            self.assertTrue(state.exists())

            snapshot_store = temporary / "snapshots"
            save = run_cli(
                "snapshot",
                "save",
                "examples/living_room.json",
                "--store",
                str(snapshot_store),
                "--version",
                "v0001",
            )
            self.assertEqual(save.returncode, 0, save.stderr)
            query = run_cli(
                "snapshot",
                "query",
                "--store",
                str(snapshot_store),
                "--project-id",
                "living-room-stereo-demo",
                "--version",
                "v0001",
                "--pose",
                "seat.sofa.center",
                "--rows",
                "0:0",
            )
            self.assertEqual(query.returncode, 0, query.stderr)
            query_payload = json.loads(query.stdout)
            self.assertEqual(len(query_payload["poses"]), 1)
            self.assertTrue(query_payload["blocks"])
            self.assertTrue(
                all(block["row"] == 0 for block in query_payload["blocks"])
            )

    def test_cli_validation_failure_is_stable_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            invalid = Path(directory) / "invalid.json"
            invalid.write_text(
                json.dumps({"schema_version": 1}),
                encoding="utf-8",
            )
            result = run_cli("validate", str(invalid))
            self.assertNotEqual(result.returncode, 0)
            payload = json.loads(result.stdout)
            self.assertFalse(payload["valid"])
            self.assertTrue(payload["issues"])
            self.assertEqual(
                payload["issues"],
                sorted(
                    payload["issues"],
                    key=lambda item: (
                        item["path"],
                        item["code"],
                        item["message"],
                    ),
                ),
            )


if __name__ == "__main__":
    unittest.main()
