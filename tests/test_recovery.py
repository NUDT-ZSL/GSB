from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ticket_gate.engine import connect_database, is_propagation_complete
from ticket_gate.parser import load_project


class RecoveryTests(unittest.TestCase):
    def test_abrupt_exit_after_committed_batch_resumes_without_replay(self) -> None:
        project = load_project(ROOT / "sample_project")
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "run.db"
            kernel = Path(temporary) / "kernel"
            reference = Path(temporary) / "reference" / "reference.json"
            command = [
                sys.executable,
                "-m",
                "ticket_gate",
                "run",
                "--project",
                str(ROOT / "sample_project"),
                "--db",
                str(database),
                "--out",
                str(kernel),
                "--reference-out",
                str(reference),
            ]

            crashed = subprocess.run(
                [*command, "--crash-after-batch", "1"],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(crashed.returncode, 75, crashed.stderr)
            self.assertFalse(is_propagation_complete(project, database))

            connection = connect_database(database)
            try:
                run_numbers = [
                    row[0]
                    for row in connection.execute(
                        "SELECT batch_no FROM batch_runs ORDER BY batch_no"
                    )
                ]
                self.assertEqual(run_numbers, [0, 1])
                retry_rows = list(
                    connection.execute(
                        "SELECT gate_id, due_batch_no FROM retry_queue ORDER BY gate_id"
                    )
                )
                self.assertEqual([tuple(row) for row in retry_rows], [("GATE-001", 2)])
                gate_one = connection.execute(
                    "SELECT state, attempt_count FROM gate_state WHERE gate_id = 'GATE-001'"
                ).fetchone()
                self.assertEqual(tuple(gate_one), ("failed", 2))
                committed_transitions = list(
                    connection.execute(
                        "SELECT transition_id, gate_id, to_state, batch_no, attempt_no "
                        "FROM gate_transitions WHERE batch_no <= 1 ORDER BY transition_id"
                    )
                )
            finally:
                connection.close()

            resumed = subprocess.run(
                command,
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
            self.assertTrue(is_propagation_complete(project, database))

            connection = connect_database(database)
            try:
                final_runs = [
                    row[0]
                    for row in connection.execute(
                        "SELECT batch_no FROM batch_runs ORDER BY batch_no"
                    )
                ]
                self.assertEqual(final_runs, list(range(9)))
                replayed_rows = list(
                    connection.execute(
                        "SELECT transition_id, gate_id, to_state, batch_no, attempt_no "
                        "FROM gate_transitions WHERE batch_no <= 1 ORDER BY transition_id"
                    )
                )
                self.assertEqual(
                    [tuple(row) for row in committed_transitions],
                    [tuple(row) for row in replayed_rows],
                )
                gate_one = connection.execute(
                    "SELECT state, attempt_count, activated_batch "
                    "FROM gate_state WHERE gate_id = 'GATE-001'"
                ).fetchone()
                self.assertEqual(tuple(gate_one), ("active", 3, 2))
                gate_four = connection.execute(
                    "SELECT state, attempt_count, active_versions_json "
                    "FROM gate_state WHERE gate_id = 'GATE-004'"
                ).fetchone()
                self.assertEqual(gate_four[0], "failed")
                self.assertEqual(gate_four[1], 1)
                self.assertEqual(
                    json.loads(gate_four[2]),
                    {"EVT-04": "v1", "EVT-07": "v1"},
                )
            finally:
                connection.close()

            summary = json.loads((kernel / "summary.json").read_text(encoding="utf-8"))
            self.assertTrue(summary["invariants"]["accounted_gate_set_equals_affected"])
            self.assertTrue(summary["invariants"]["retry_queue_is_empty"])
            self.assertEqual(summary["degradation"]["gate_ids"], ["GATE-004"])
            comparison = json.loads(
                (kernel / "compare.json").read_text(encoding="utf-8")
            )
            self.assertTrue(comparison["ok"], json.dumps(comparison, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    unittest.main()
