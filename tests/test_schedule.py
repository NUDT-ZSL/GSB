from __future__ import annotations

import json
import sqlite3
import unittest

from topodiag.engine import build_workspace
from topodiag.sample import generate_samples
from topodiag.scheduler import schedule_workspace
from topodiag.storage import runtime_db_path

from .helpers import make_workdir, status, verify


class ScheduleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workdir = make_workdir("topodiag-schedule-")
        generate_samples(self.workdir)
        build_workspace(self.workdir)
        self.result = schedule_workspace(
            self.workdir,
            round_budget_ms=250,
            workers=4,
        )

    def test_expected_outcome_classes(self) -> None:
        self.assertEqual(
            self.result["summary"],
            {
                "abandoned": 1,
                "completed": 11,
                "not_started": 1,
                "rejected": 4,
                "started_failed": 1,
            },
        )
        by_seq = {row["request_seq"]: row for row in self.result["outcomes"]}
        self.assertEqual(by_seq[3]["reason_code"], "TEMPLATE_CONCURRENCY_EXCEEDED")
        self.assertEqual(by_seq[9]["reason_code"], "TOPOLOGY_BREAK")
        self.assertEqual(by_seq[14]["outcome"], "started_failed")
        self.assertEqual(by_seq[15]["outcome"], "abandoned")
        self.assertEqual(by_seq[16]["reason_code"], "RESOURCE_INSUFFICIENT")
        self.assertEqual(by_seq[18]["outcome"], "not_started")
        self.assertEqual(by_seq[18]["reason_code"], "ROUND_BUDGET_EXHAUSTED")

    def test_status_and_verify_are_consistent(self) -> None:
        status_payload = status(self.workdir)
        verify_payload = verify(self.workdir)
        self.assertTrue(verify_payload["ok"], verify_payload["errors"])
        self.assertEqual(
            status_payload["semantic_digest"],
            verify_payload["semantic_digest"],
        )
        self.assertEqual(
            status_payload["watermarks"]["committed_instance_watermark"],
            13,
        )
        self.assertEqual(
            status_payload["watermarks"]["logical_clock_ms"],
            12_930,
        )
        self.assertEqual(len(status_payload["closures"]), 10)
        self.assertEqual(len(status_payload["resources"]), 6)

    def test_resource_decision_records_observations_and_before_values(self) -> None:
        db = runtime_db_path(self.workdir)
        with sqlite3.connect(str(db)) as connection:
            connection.row_factory = sqlite3.Row
            rejected = connection.execute(
                """
                SELECT decision_json, reason_code
                FROM request_state WHERE request_seq=16
                """
            ).fetchone()
            decision = json.loads(rejected["decision_json"])
            self.assertEqual(rejected["reason_code"], "RESOURCE_INSUFFICIENT")
            observations = decision["resource_plan"]["resource_observations"]
            self.assertTrue(observations)
            self.assertTrue(
                all("used_before" in item for item in observations)
            )
            self.assertEqual(
                decision["first_constraint"]["constraint"],
                "node_resource_budget",
            )
            graph = decision["topology"]["graph"]
            self.assertEqual(graph["root_template"], "frontend")
            self.assertGreater(graph["node_count"], 1)
            self.assertTrue(graph["edges"])
            events = connection.execute(
                """
                SELECT before_value, delta, after_value
                FROM ledger_events
                ORDER BY event_id
                """
            ).fetchall()
            self.assertTrue(events)
            self.assertTrue(
                all(
                    row["after_value"] == row["before_value"] + row["delta"]
                    for row in events
                )
            )

    def test_repeat_schedule_is_idempotent(self) -> None:
        before = status(self.workdir)
        second = schedule_workspace(
            self.workdir,
            round_budget_ms=250,
            workers=4,
        )
        after = status(self.workdir)
        self.assertEqual(second["summary"], self.result["summary"])
        self.assertEqual(before["semantic_digest"], after["semantic_digest"])
        self.assertEqual(before["instances"], after["instances"])
        self.assertEqual(before["resources"], after["resources"])


if __name__ == "__main__":
    unittest.main()
