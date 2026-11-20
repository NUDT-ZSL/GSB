from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable


def invoke(
    *args: str,
    env_extra: dict[str, str] | None = None,
) -> tuple[dict[str, Any], int, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    if env_extra:
        env.update(env_extra)
    completed = subprocess.run(
        [PYTHON, "-m", "eventbus", *args],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=90,
    )
    parsed = json.loads(completed.stdout) if completed.stdout.strip() else {}
    return parsed, completed.returncode, completed.stdout


def ok(*args: str) -> dict[str, Any]:
    result, code, _ = invoke(*args)
    if code != 0:
        raise AssertionError(f"command failed: {args}: {code}: {result}")
    return result


def failed(code: str, *args: str) -> dict[str, Any]:
    result, exit_code, _ = invoke(*args)
    if exit_code == 0:
        raise AssertionError(f"command unexpectedly succeeded: {args}: {result}")
    actual = result.get("error", {}).get("code")
    if actual != code:
        raise AssertionError(f"expected {code}, got {actual}: {result}")
    return result


class EventBusTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="eventbus-test-")
        self.root = Path(self.temp.name)
        self.db = self.root / "bus.db"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def load(self, clock: int = 11000) -> dict[str, Any]:
        return ok(
            "load",
            "--db",
            str(self.db),
            "--samples",
            str(ROOT / "samples"),
            "--clock",
            str(clock),
        )

    def status(self) -> dict[str, Any]:
        return ok("status", "--db", str(self.db))

    def test_invalid_samples_write_no_database(self) -> None:
        cases = [
            (
                "duplicate_channel",
                lambda data: data["channels"].append(dict(data["channels"][0])),
                "E_DUPLICATE_CHANNEL",
            ),
            (
                "bad_partitions",
                lambda data: data["channels"][0].update({"partitions": 0}),
                "E_BAD_PARTITIONS",
            ),
        ]
        for name, mutate, error_code in cases:
            with self.subTest(name=name):
                sample_dir = self.root / f"samples-{name}"
                shutil.copytree(ROOT / "samples", sample_dir)
                catalog_path = sample_dir / "catalog.json"
                catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
                mutate(catalog)
                catalog_path.write_text(
                    json.dumps(catalog), encoding="utf-8"
                )
                db = self.root / f"{name}.db"
                failed(
                    error_code,
                    "load",
                    "--db",
                    str(db),
                    "--samples",
                    str(sample_dir),
                    "--clock",
                    "11000",
                )
                self.assertFalse(db.exists())

    def test_bridge_cycle_and_event_sequence_are_rejected_before_persistence(self) -> None:
        cycle_dir = self.root / "samples-cycle"
        shutil.copytree(ROOT / "samples", cycle_dir)
        bridges_path = cycle_dir / "bridges.json"
        bridges = json.loads(bridges_path.read_text(encoding="utf-8"))
        bridges["rules"].append(
            {
                "id": "audit-to-orders",
                "upstream": "audit-main",
                "downstream": "orders-main",
                "filter": "True",
                "transform": "{\"kind\": \"cycle\"}",
                "depends_upstream_offset": 0,
            }
        )
        bridges_path.write_text(json.dumps(bridges), encoding="utf-8")
        failed(
            "E_BRIDGE_CYCLE",
            "load",
            "--db",
            str(self.db),
            "--samples",
            str(cycle_dir),
            "--clock",
            "11000",
        )
        self.assertFalse(self.db.exists())

        seq_dir = self.root / "samples-seq"
        shutil.copytree(ROOT / "samples", seq_dir)
        events_path = seq_dir / "events.json"
        events = json.loads(events_path.read_text(encoding="utf-8"))
        events["events"][1]["seq"] = 1
        events_path.write_text(json.dumps(events), encoding="utf-8")
        failed(
            "E_EVENT_SEQ",
            "load",
            "--db",
            str(self.db),
            "--samples",
            str(seq_dir),
            "--clock",
            "11000",
        )
        self.assertFalse(self.db.exists())

    def test_end_to_end_bridge_consume_cleanup_and_verify(self) -> None:
        load = self.load()
        self.assertEqual(load["appended"], 8)
        bridge = ok(
            "bridge", "--db", str(self.db), "--clock", "12000"
        )
        self.assertEqual(
            sum(rule["forwarded"] for rule in bridge["rules"]), 7
        )
        status_before, _, raw_before = invoke(
            "status", "--db", str(self.db)
        )
        _, _, raw_again = invoke("status", "--db", str(self.db))
        self.assertEqual(raw_before, raw_again)
        channels = {item["channel"]: item for item in status_before["channels"]}
        self.assertEqual(channels["orders-main"]["end_offset"], 5)
        self.assertEqual(channels["payments-main"]["end_offset"], 4)
        self.assertEqual(channels["audit-main"]["end_offset"], 6)

        for _ in range(20):
            status = self.status()
            active = [
                item
                for item in status["subscriptions"]
                if int(item["lag"]) > 0
            ]
            if not active:
                break
            for item in active:
                ok(
                    "consume",
                    "--db",
                    str(self.db),
                    "--consumer",
                    item["consumer_id"],
                    "--clock",
                    "14000",
                )
        else:
            self.fail("subscriptions did not drain")

        ok(
            "cleanup", "--db", str(self.db), "--clock", "21000"
        )
        conn = sqlite3.connect(self.db)
        try:
            cleanup_runs = [
                json.loads(row[0])
                for row in conn.execute(
                    "SELECT summary_json FROM cleanup_runs ORDER BY run_id"
                )
            ]
        finally:
            conn.close()
        self.assertGreater(
            sum(
                item["physical_cleared"]
                for run in cleanup_runs
                for item in run["channels"]
            ),
            0,
        )
        self.assertGreater(
            sum(
                item["tombstoned"]
                for run in cleanup_runs
                for item in run["channels"]
            ),
            0,
        )
        verify = ok(
            "verify",
            "--db",
            str(self.db),
            "--samples",
            str(ROOT / "samples"),
        )
        self.assertTrue(verify["ok"])
        self.assertTrue(all(verify["checks"].values()))
        final_status = self.status()
        self.assertGreater(
            sum(
                item["tombstones"]
                for item in final_status["channels"]
            ),
            0,
        )
        status_path = self.root / "status.json"
        status_path.write_text(
            json.dumps(final_status, sort_keys=True) + "\n", encoding="utf-8"
        )
        ok(
            "verify",
            "--db",
            str(self.db),
            "--samples",
            str(ROOT / "samples"),
            "--status-file",
            str(status_path),
        )
        final_status["channels"][0]["end_offset"] += 1
        status_path.write_text(
            json.dumps(final_status, sort_keys=True) + "\n", encoding="utf-8"
        )
        tampered, code, _ = invoke(
            "verify",
            "--db",
            str(self.db),
            "--samples",
            str(ROOT / "samples"),
            "--status-file",
            str(status_path),
        )
        self.assertNotEqual(code, 0)
        self.assertFalse(tampered["ok"])

    def test_checkpoint_reports_are_monotonic_and_idempotent(self) -> None:
        self.load()
        first = ok(
            "consume",
            "--db",
            str(self.db),
            "--consumer",
            "ledger",
            "--clock",
            "14000",
            "--budget",
            "1",
        )
        self.assertEqual(first["offset_before"], 0)
        self.assertEqual(first["offset_after"], 1)
        stale = ok(
            "consume",
            "--db",
            str(self.db),
            "--consumer",
            "ledger",
            "--clock",
            "14000",
            "--report",
            "0",
        )
        self.assertEqual(stale["outcome"], "stale_report")
        self.assertEqual(stale["offset_after"], 1)
        duplicate = ok(
            "consume",
            "--db",
            str(self.db),
            "--consumer",
            "ledger",
            "--clock",
            "14000",
            "--report",
            "1",
        )
        self.assertEqual(duplicate["outcome"], "duplicate_report")
        self.assertEqual(duplicate["offset_after"], 1)
        failed(
            "E_OFFSET_AHEAD",
            "consume",
            "--db",
            str(self.db),
            "--consumer",
            "ledger",
            "--clock",
            "14000",
            "--report",
            "999",
        )
        failed(
            "E_SUBSCRIPTION",
            "consume",
            "--db",
            str(self.db),
            "--consumer",
            "unknown",
            "--clock",
            "14000",
        )
        self.assertEqual(
            {
                item["consumer_id"]: item["current_offset"]
                for item in self.status()["subscriptions"]
            }["ledger"],
            1,
        )

    def test_clock_and_sample_digest_failures_do_not_change_state(self) -> None:
        self.load()
        before = self.status()
        failed(
            "E_CLOCK_REGRESSION",
            "cleanup",
            "--db",
            str(self.db),
            "--clock",
            "10000",
        )
        failed(
            "E_SAMPLE_DIGEST",
            "cleanup",
            "--db",
            str(self.db),
            "--clock",
            "12000",
            "--expect-sample-digest",
            "0" * 64,
        )
        after = self.status()
        self.assertEqual(before["state_digest"], after["state_digest"])

    def test_invalid_edit_does_not_create_operation_state(self) -> None:
        self.load()
        before = self.status()
        failed(
            "E_EVENT_SEQ",
            "edit",
            "--db",
            str(self.db),
            "--clock",
            "18000",
            "--kind",
            "event",
            "--action",
            "add",
            "--data",
            json.dumps(
                {
                    "seq": 1,
                    "topic": "orders",
                    "partition_key": "customer-a",
                    "payload": {"kind": "duplicate"},
                    "clock_ms": 1800,
                }
            ),
        )
        after = self.status()
        self.assertEqual(before["state_digest"], after["state_digest"])

    def test_edit_versions_and_config_snapshot_switch(self) -> None:
        self.load()
        event_edit = ok(
            "edit",
            "--db",
            str(self.db),
            "--clock",
            "18000",
            "--kind",
            "event",
            "--action",
            "add",
            "--data",
            json.dumps(
                {
                    "seq": 9,
                    "topic": "orders",
                    "partition_key": "customer-z",
                    "payload": {
                        "amount": 250,
                        "kind": "order.created",
                        "order_id": "o-9",
                    },
                    "clock_ms": 1490,
                }
            ),
        )
        self.assertEqual(event_edit["edit_version"], 1)
        self.assertIn("orders-main", event_edit["impact_closure"]["channels"])
        publish = ok(
            "publish", "--db", str(self.db), "--clock", "18050"
        )
        self.assertEqual(publish["appended"], 1)

        rule_edit = ok(
            "edit",
            "--db",
            str(self.db),
            "--clock",
            "19000",
            "--kind",
            "rule",
            "--action",
            "update",
            "--id",
            "orders-to-audit",
            "--data",
            json.dumps({"depends_upstream_offset": 2}),
        )
        self.assertEqual(rule_edit["config_version"], 2)
        self.assertEqual(rule_edit["edit_version"], 2)

        conn = sqlite3.connect(self.db)
        try:
            pointer = conn.execute(
                "SELECT version FROM config_pointer WHERE id=1"
            ).fetchone()[0]
            versions = conn.execute(
                "SELECT COUNT(*) FROM config_versions"
            ).fetchone()[0]
            edit_versions = conn.execute(
                "SELECT COUNT(*) FROM edit_versions"
            ).fetchone()[0]
            published = conn.execute(
                "SELECT COUNT(*) FROM published_events"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(pointer, 2)
        self.assertEqual(versions, 2)
        self.assertEqual(edit_versions, 2)
        self.assertEqual(published, 9)
        self.assertTrue(
            ok(
                "verify",
                "--db",
                str(self.db),
                "--samples",
                str(ROOT / "samples"),
            )["ok"]
        )

    def test_verify_detects_log_corruption(self) -> None:
        self.load()
        conn = sqlite3.connect(self.db)
        try:
            conn.execute(
                "UPDATE records SET payload_json=? WHERE channel='orders-main' "
                "AND offset=0",
                ('{"broken":true}',),
            )
            conn.commit()
        finally:
            conn.close()
        result, code, _ = invoke(
            "verify", "--db", str(self.db), "--samples", str(ROOT / "samples")
        )
        self.assertNotEqual(code, 0)
        self.assertFalse(result["ok"])
        self.assertTrue(
            any(
                "payload digest mismatch" in item["message"]
                for item in result["errors"]
            )
        )

    def test_crash_recovery_script(self) -> None:
        completed = subprocess.run(
            [PYTHON, str(ROOT / "scripts" / "crash_recovery.py")],
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=120,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
        result = json.loads(completed.stdout)
        self.assertTrue(result["ok"])

    def test_fixed_seed_concurrency_script(self) -> None:
        completed = subprocess.run(
            [PYTHON, str(ROOT / "scripts" / "concurrency_check.py")],
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=120,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
        result = json.loads(completed.stdout)
        self.assertTrue(result["ok"])
        self.assertEqual(result["duplicate_offsets"], 0)
        self.assertEqual(result["duplicate_bridge_actions"], 0)

    def test_two_fixed_sample_runs_are_byte_identical(self) -> None:
        completed = subprocess.run(
            [
                PYTHON,
                str(ROOT / "scripts" / "repeat_verify.py"),
                "--repo",
                str(ROOT),
                "--python",
                PYTHON,
                "--output",
                str(self.root / "repeat"),
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=180,
        )
        self.assertEqual(
            completed.returncode, 0, completed.stderr + completed.stdout
        )
        result = json.loads(completed.stdout)
        self.assertTrue(result["ok"])
        self.assertTrue(result["byte_identical"])
        self.assertEqual(
            result["first"]["transcript_sha256"],
            result["second"]["transcript_sha256"],
        )


if __name__ == "__main__":
    unittest.main()

