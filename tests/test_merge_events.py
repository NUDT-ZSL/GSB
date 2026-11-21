from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tests.helpers import (
    copy_sample,
    make_tiny_v1_v2_tie_package,
    read_json,
    run_cli,
    write_json,
)


class MergeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.package = copy_sample(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_t0_state_and_migration_provenance(self) -> None:
        db = self.root / "state.sqlite"
        run_cli(db, self.package, "merge", "--clock-step", "t0", check=True)
        _, status = run_cli(db, self.package, "status", check=True)
        expected = read_json(self.package.parent / "expected.json")["expected"]["t0_states"]
        actual = {
            f"{service['service']}/{service['environment']}": service["state"]
            for service in status["view"]["services"]
        }
        self.assertEqual(actual, expected)

        billing = next(
            service
            for service in status["view"]["services"]
            if service["service"] == "billing"
        )
        self.assertEqual(billing["active_config"]["version"], "1.5.0")
        orders_dev = next(
            service
            for service in status["view"]["services"]
            if service["service"] == "orders" and service["environment"] == "dev"
        )
        provenance = orders_dev["active_config"]["param_provenance"]["replicas"]
        self.assertEqual(provenance["original_time"]["kind"], "relative_seconds")
        self.assertEqual(provenance["original_time"]["scale_ms_per_second"], 1000)
        self.assertEqual(
            provenance["original_time"]["rounding_direction"],
            "toward_positive_infinity",
        )
        migration = next(
            item
            for item in status["migration_decisions"]
            if item["service"] == "billing" and item["environment"] == "prod"
        )
        self.assertEqual(migration["effective_at_ms"], 6000)
        self.assertEqual(migration["source_relative_seconds"], 5)
        self.assertEqual(migration["scale_ms_per_second"], 1000)

    def test_format_priority_breaks_equal_clock_tie(self) -> None:
        package = make_tiny_v1_v2_tie_package(self.root / "tie")
        db = self.root / "tie.sqlite"
        _, merge = run_cli(db, package, "merge", "--clock-step", "t0", check=True)
        candidates = merge["arbitration"][0]["candidates"]
        winner = next(item for item in candidates if item["winner"])
        loser = next(item for item in candidates if not item["winner"])
        self.assertEqual(winner["source_format"], "v2")
        self.assertEqual(loser["reason"], "LOWER_SOURCE_PRIORITY")
        _, status = run_cli(db, package, "status", check=True)
        active = status["view"]["services"][0]["active_config"]
        self.assertEqual(active["source_format"], "v2")
        self.assertEqual(active["params"]["winner"], "v2")

    def test_repeated_run_is_byte_identical_and_idempotent(self) -> None:
        db1 = self.root / "one.sqlite"
        db2 = self.root / "two.sqlite"
        outputs = []
        for db in (db1, db2):
            run_cli(db, self.package, "merge", "--clock-step", "t0", check=True)
            _, status = run_cli(db, self.package, "status", check=True)
            outputs.append(json.dumps(status, sort_keys=True))
        self.assertEqual(outputs[0], outputs[1])

        _, first = run_cli(db1, self.package, "merge", "--clock-step", "t0", check=True)
        _, second = run_cli(db1, self.package, "merge", "--clock-step", "t0", check=True)
        self.assertEqual(first, second)

    def test_clock_rollback_preserves_view(self) -> None:
        db = self.root / "state.sqlite"
        _, first_merge = run_cli(
            db, self.package, "merge", "--clock-step", "t0", check=True
        )
        run_cli(db, self.package, "reconcile", "--clock-step", "t3", check=True)
        _, before = run_cli(db, self.package, "status", check=True)
        exit_code, failure = run_cli(
            db, self.package, "reconcile", "--clock-ms", "1000"
        )
        self.assertEqual(exit_code, 2)
        self.assertEqual(failure["code"], "CLOCK_ROLLBACK")
        _, after = run_cli(db, self.package, "status", check=True)
        self.assertEqual(before["view"], after["view"])
        _, repeated_merge = run_cli(
            db, self.package, "merge", "--clock-step", "t0", check=True
        )
        self.assertEqual(repeated_merge, first_merge)


class EventTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.package = copy_sample(self.root)
        self.db = self.root / "state.sqlite"
        run_cli(self.db, self.package, "merge", "--clock-step", "t0", check=True)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_event_evolution_conflicts_tombstones_and_expiry(self) -> None:
        _, reconcile = run_cli(
            self.db, self.package, "reconcile", "--clock-step", "t3", check=True
        )
        outcomes = {
            (item["source"], item["seq"]): item for item in reconcile["event_outcomes"]
        }
        self.assertEqual(outcomes[("auto", 1)]["status"], "rejected")
        self.assertEqual(outcomes[("auto", 1)]["reason"], "LOWER_VERSION")
        self.assertEqual(outcomes[("manual", 2)]["status"], "rejected")
        self.assertEqual(outcomes[("manual", 2)]["reason"], "LOWER_VERSION")

        _, status = run_cli(self.db, self.package, "status", check=True)
        expected = read_json(self.package.parent / "expected.json")["expected"]["t3_states"]
        actual = {
            f"{service['service']}/{service['environment']}": service["state"]
            for service in status["view"]["services"]
        }
        self.assertEqual(actual, expected)
        versions = {
            f"{service['service']}/{service['environment']}": (
                service["active_config"]["version"] if service["active_config"] else None
            )
            for service in status["view"]["services"]
        }
        self.assertEqual(versions["orders/dev"], "2.1.0")
        self.assertEqual(versions["orders/prod"], "2.3.0")
        self.assertEqual(versions["cache/dev"], "2.0.0")
        streams = {
            item["source"]: (item["committed_seq"], item["watermark_clock_ms"])
            for item in status["event_streams"]
        }
        self.assertEqual(streams["manual"], (3, 2800))
        self.assertEqual(streams["auto"], (4, 3000))
        active_keys = {
            (item["service"], item["environment"])
            for item in status["view"]["effective_services"]
        }
        self.assertNotIn(("billing", "prod"), active_keys)
        self.assertNotIn(("search", "staging"), active_keys)
        self.assertNotIn(("legacy", "prod"), active_keys)

    def test_future_event_does_not_affect_view(self) -> None:
        _, reconcile = run_cli(
            self.db, self.package, "reconcile", "--clock-step", "t2", check=True
        )
        self.assertTrue(
            any(
                item["event_id"] == "auto-orders-prod-2.3.0"
                for item in reconcile["deferred_events"]
            )
        )
        _, status = run_cli(self.db, self.package, "status", check=True)
        orders = next(
            service
            for service in status["view"]["services"]
            if service["service"] == "orders" and service["environment"] == "prod"
        )
        self.assertEqual(orders["state"], "active")
        self.assertEqual(orders["active_config"]["version"], "2.0.0")
        self.assertIn("2.2.0", [item["version"] for item in orders["future_versions"]])
        streams = {
            item["source"]: item["committed_seq"] for item in status["event_streams"]
        }
        self.assertEqual(streams["auto"], 1)
        self.assertEqual(streams["manual"], 2)

    def test_unknown_target_does_not_block_other_services(self) -> None:
        path = self.package.parent / "events" / "auto.json"
        document = read_json(path)
        document["events"][2]["service"] = "ghost"
        write_json(path, document)
        _, reconcile = run_cli(
            self.db, self.package, "reconcile", "--clock-step", "t3", check=True
        )
        outcomes = {
            (item["source"], item["seq"]): item for item in reconcile["event_outcomes"]
        }
        self.assertEqual(outcomes[("auto", 3)]["status"], "rejected")
        self.assertEqual(outcomes[("auto", 3)]["reason"], "EVENT_TARGET_UNKNOWN")
        self.assertEqual(outcomes[("auto", 4)]["status"], "accepted")
        _, status = run_cli(self.db, self.package, "status", check=True)
        streams = {
            item["source"]: item["committed_seq"] for item in status["event_streams"]
        }
        self.assertEqual(streams["auto"], 4)

    def test_content_digest_mismatch_does_not_block_other_events(self) -> None:
        path = self.package.parent / "events" / "auto.json"
        document = read_json(path)
        document["events"][2]["params"]["issuer"] = "tampered"
        write_json(path, document)
        _, reconcile = run_cli(
            self.db, self.package, "reconcile", "--clock-step", "t3", check=True
        )
        outcomes = {
            (item["source"], item["seq"]): item for item in reconcile["event_outcomes"]
        }
        self.assertEqual(outcomes[("auto", 3)]["status"], "rejected")
        self.assertEqual(outcomes[("auto", 3)]["reason"], "CONTENT_DIGEST_MISMATCH")
        self.assertEqual(outcomes[("auto", 4)]["status"], "accepted")


class VerifyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.package = copy_sample(self.root)
        self.db = self.root / "state.sqlite"
        run_cli(self.db, self.package, "merge", "--clock-step", "t0", check=True)
        run_cli(self.db, self.package, "reconcile", "--clock-step", "t3", check=True)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_verify_detects_tampered_view(self) -> None:
        conn = sqlite3.connect(self.db)
        try:
            row = conn.execute(
                "SELECT view_seq, payload_json FROM views ORDER BY view_seq DESC LIMIT 1"
            ).fetchone()
            payload = json.loads(row[1])
            payload["clock_ms"] += 1
            conn.execute(
                "UPDATE views SET payload_json=? WHERE view_seq=?",
                (json.dumps(payload, sort_keys=True, separators=(",", ":")), row[0]),
            )
            conn.commit()
        finally:
            conn.close()
        exit_code, report = run_cli(self.db, self.package, "verify")
        self.assertEqual(exit_code, 1)
        self.assertFalse(report["ok"])
        failed = {
            item["details"]["check"]
            for item in report["errors"]
            if "check" in item.get("details", {})
        }
        self.assertIn("stored_view_hash_matches", failed)
        self.assertIn("view_recomputes_exactly", failed)


if __name__ == "__main__":
    unittest.main()

