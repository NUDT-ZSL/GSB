from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.helpers import run_cli_json, setup_full_state


class ReproducibilityTests(unittest.TestCase):
    def _run(self, state: Path) -> list[dict]:
        outputs = setup_full_state(state, gc=True)
        for command in ("status", "verify"):
            result, payload = run_cli_json(state, command)
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertIsNotNone(payload)
            outputs.append(payload)
        return outputs

    def test_two_independent_runs_have_identical_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = self._run(root / "first")
            second = self._run(root / "second")
            self.assertEqual(first, second)

    def test_status_output_is_stable_when_repeated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            setup_full_state(state, gc=True)
            first_result, first = run_cli_json(state, "status")
            second_result, second = run_cli_json(state, "status")
            self.assertEqual(first_result.returncode, 0)
            self.assertEqual(second_result.returncode, 0)
            self.assertEqual(first, second)
