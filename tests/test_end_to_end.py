from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ticket_gate.compare import compare_documents
from ticket_gate.engine import (
    build_report,
    prepare_database,
    propagate,
    verify_requests,
)
from ticket_gate.evaluator import (
    REASON_AFTER_WINDOW,
    REASON_ALLOWED,
    REASON_BEFORE_WINDOW,
    REASON_CHILD_ADULT,
    REASON_REENTRY,
    REASON_TICKET_NOT_WHITELISTED,
)
from ticket_gate.parser import load_project


class EndToEndTests(unittest.TestCase):
    def test_propagation_verification_report_and_reference_agree(self) -> None:
        project = load_project(ROOT / "sample_project")
        self.assertTrue(project.valid)
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "run.db"
            reference_path = Path(temporary) / "reference.json"

            plan = prepare_database(project, database, reset=True)
            first = propagate(project, database)
            self.assertTrue(first["complete"])
            self.assertEqual(first["processed_batches"], list(range(9)))
            second = propagate(project, database)
            self.assertEqual(second["processed_batches"], [])
            self.assertTrue(second["complete"])

            decisions = verify_requests(project, database)
            self.assertEqual(len(decisions), 126)
            report = build_report(project, database)
            self.assertTrue(all(report["invariants"].values()))
            self.assertEqual(report["affected_gate_ids"], list(plan.affected_gate_ids))
            self.assertEqual(
                report["accounted_gate_ids"],
                report["affected_gate_ids"],
            )
            self.assertEqual(report["degradation"]["gate_ids"], ["GATE-004"])
            gate_four = next(
                gate for gate in report["gate_versions"] if gate["gate_id"] == "GATE-004"
            )
            self.assertEqual(gate_four["state"], "failed")
            self.assertEqual(
                gate_four["active_versions"],
                {"EVT-04": "v1", "EVT-07": "v1"},
            )
            self.assertFalse(gate_four["target_acknowledged"])

            decisions = report["verification"]["decisions"]
            observed_reasons = {
                decision["reason_code"] for decision in decisions
            }
            self.assertEqual(
                observed_reasons,
                {
                    REASON_ALLOWED,
                    REASON_TICKET_NOT_WHITELISTED,
                    REASON_BEFORE_WINDOW,
                    REASON_AFTER_WINDOW,
                    REASON_REENTRY,
                    REASON_CHILD_ADULT,
                },
            )
            for decision in decisions:
                if decision["decision"] == "REJECT":
                    self.assertIsNotNone(decision["trace"]["decisive"])
                    self.assertTrue(
                        decision["trace"]["decisive"]["source"].startswith(
                            "config/events.json#"
                        )
                    )
            self.assertEqual(
                report["verification"]["stats"]["total"],
                len(decisions),
            )
            self.assertGreater(report["verification"]["stats"]["degraded_decisions"], 0)

            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "reference_judge.py"),
                    "--project",
                    str(ROOT / "sample_project"),
                    "--out",
                    str(reference_path),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            reference = json.loads(reference_path.read_text(encoding="utf-8"))
            comparison = compare_documents(report, reference)
            self.assertTrue(comparison["ok"], json.dumps(comparison, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    unittest.main()
