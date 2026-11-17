from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from alarm_domain.demo import run_demo
from alarm_domain.diagnostics import build_diagnostic_model, render_diagnostic
from alarm_domain.session import AlarmSession
from alarm_domain.util import canonical_json

from .helpers import EVENTS, RULES, TOPOLOGY


class DemoAndDiagnosticsTests(unittest.TestCase):
    def test_demo_is_deterministic_and_covers_three_scenarios(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "demo"
            first = run_demo(work_dir=work)
            first_bytes = canonical_json(first)
            second = run_demo(work_dir=work)
            second_bytes = canonical_json(second)
            log_lines = (work / "structured.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(first_bytes, second_bytes)
        self.assertEqual(
            [item["scenario"] for item in first["scenarios"]],
            ["cold-load", "incremental-rule-update", "snapshot-restore-replay"],
        )
        self.assertTrue(first["scenarios"][2]["summary"]["restore"]["replay"]["match"])
        self.assertTrue(any(json.loads(line)["event"] == "snapshot_restore" for line in log_lines))

    def test_anomaly_injections_report_expected_codes_and_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            time_result = run_demo(work_dir=Path(tmp) / "time", inject_time_regression=True)
            snap_result = run_demo(
                work_dir=Path(tmp) / "snap", inject_missing_snapshot_version=True
            )
            time_log = (Path(tmp) / "time" / "anomaly-structured.jsonl").read_text(encoding="utf-8")
            snap_log = (Path(tmp) / "snap" / "anomaly-structured.jsonl").read_text(encoding="utf-8")
        self.assertEqual(time_result["scenarios"][0]["code"], "E_TIME")
        self.assertEqual(time_result["scenarios"][0]["line"], 4)
        self.assertEqual(snap_result["scenarios"][0]["code"], "E_SNAP")
        self.assertIn('"code":"E_TIME"', time_log)
        self.assertIn('"code":"E_SNAP"', snap_log)

    def test_diagnostic_projection_has_stable_render_order(self) -> None:
        session = AlarmSession.from_files(RULES, TOPOLOGY, EVENTS, window_ms=500)
        model = build_diagnostic_model(session)
        self.assertEqual([rule["rule_id"] for rule in model["rules"]], sorted(rule["rule_id"] for rule in model["rules"]))
        self.assertEqual(
            [(edge["upstream"], edge["downstream"]) for edge in model["edges"]],
            sorted((edge["upstream"], edge["downstream"]) for edge in model["edges"]),
        )
        self.assertEqual(
            [(cluster["start_ts"], cluster["cluster_id"]) for cluster in model["clusters"]],
            sorted((cluster["start_ts"], cluster["cluster_id"]) for cluster in model["clusters"]),
        )
        for cluster in model["clusters"]:
            self.assertEqual(
                [candidate["rank"] for candidate in cluster["candidates"]],
                list(range(1, len(cluster["candidates"]) + 1)),
            )
        first = render_diagnostic(model)
        second = render_diagnostic(build_diagnostic_model(session))
        self.assertEqual(first, second)
        self.assertIn("root_reason", first)
        self.assertIn("latest_trace", first)


if __name__ == "__main__":
    unittest.main()
