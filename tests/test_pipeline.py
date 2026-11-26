from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from seqledger.engine import acknowledge, route_partition
from seqledger.sample import generate_fixtures
from seqledger.store import LedgerStore
from seqledger.validation import validate_topology

from .helpers import (
    cli,
    fixture_paths,
    load_args,
    merge_args,
    prepare_merged_db,
    replay_args,
)


class PipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tempdir = tempfile.TemporaryDirectory()
        cls.fixture_dir = Path(cls.tempdir.name) / "fixtures"
        generate_fixtures(cls.fixture_dir)
        cls.paths = fixture_paths(cls.fixture_dir)
        cls.topology = validate_topology(
            json.loads(Path(cls.paths["topology"]).read_text(encoding="utf-8"))
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tempdir.cleanup()

    def test_load_is_deterministic_idempotent_and_offsets_are_contiguous(self) -> None:
        db = str(Path(self.tempdir.name) / "load-idempotent.sqlite3")
        first = json.loads(cli(*load_args(self.paths, db=db), check=True).stdout)
        version = first["ledger_version"]
        second = json.loads(cli(*load_args(self.paths, db=db), check=True).stdout)
        self.assertEqual(second["appended_events"], 0)
        self.assertEqual(second["idempotent_skips"], 18)
        self.assertEqual(second["ledger_version"], version)

        store = LedgerStore(db)
        state, _ = store.read_state()
        self.assertEqual(len(state["event_index"]), 18)
        for pid, partition in state["partitions"].items():
            self.assertEqual(
                [event["offset"] for event in partition["offsets"]],
                list(range(len(partition["offsets"]))),
            )
            for event in partition["offsets"]:
                expected_partition, digest = route_partition(
                    next(
                        topic
                        for topic in self.topology["topics"]
                        if topic["name"] == partition["topic"]
                    ),
                    event["partition_key"],
                )
                self.assertEqual(expected_partition, partition["partition"])
                self.assertEqual(digest, event["routing_sha256"])
                self.assertEqual(event["payload_digest"], event["payload_digest"].lower())

    def test_replay_completes_and_repeat_is_idempotent(self) -> None:
        db = str(Path(self.tempdir.name) / "replay-idempotent.sqlite3")
        prepare_merged_db(db, self.paths)
        first = json.loads(cli(*replay_args(db=db), check=True).stdout)
        self.assertTrue(first["completed"])
        self.assertEqual(first["backlog_depth"], 0)
        status = json.loads(cli("--db", db, "status").stdout)
        self.assertEqual(status["totals"]["backlog_depth"], 0)
        self.assertEqual(status["totals"]["inflight_count"], 0)
        self.assertEqual(status["totals"]["dead_letter_count"], 1)
        self.assertEqual(status["totals"]["delivered_decision_count"], 18)
        second = json.loads(cli(*replay_args(db=db), check=True).stdout)
        self.assertEqual(second["action_count"], 0)
        self.assertEqual(second["ledger_version_before"], second["ledger_version_after"])
        self.assertEqual(second["backlog_depth"], 0)

    def test_partition_filter_status_shape_and_stable_order(self) -> None:
        db = str(Path(self.tempdir.name) / "partition-filter.sqlite3")
        prepare_merged_db(db, self.paths)
        result = json.loads(
            cli(
                *replay_args(
                    db=db,
                    partitions=["orders/0", "orders/1", "orders/2", "orders/3"],
                ),
                check=True,
            ).stdout
        )
        self.assertTrue(
            all(partition["partition_id"].startswith("orders/") for partition in result["partitions"])
        )
        self.assertTrue(
            [p["partition_id"] for p in result["partitions"]]
            == sorted(p["partition_id"] for p in result["partitions"])
        )
        status = json.loads(cli("--db", db, "status").stdout)
        self.assertEqual(
            [partition["partition_id"] for partition in status["partitions"]],
            sorted(partition["partition_id"] for partition in status["partitions"]),
        )
        payments = [
            partition
            for partition in status["partitions"]
            if partition["topic"] == "payments"
        ]
        self.assertTrue(any(partition["backlog_depth"] > 0 for partition in payments))

    def test_acknowledgment_is_idempotent_after_sigkill_safe_inflight(self) -> None:
        db = str(Path(self.tempdir.name) / "ack-idempotent.sqlite3")
        prepare_merged_db(db, self.paths)
        replay = json.loads(
            cli(
                "--db",
                db,
                "replay",
                "--logical-clock-ms",
                "7000",
                "--max-steps",
                "1",
            ).stdout
        )
        self.assertFalse(replay["completed"])
        state, _ = LedgerStore(db).read_state()
        inflight = next(
            partition["inflight"]
            for partition in state["partitions"].values()
            if partition["inflight"] is not None
        )
        store = LedgerStore(db)
        first = acknowledge(
            store,
            event_seq=int(inflight["event_seq"]),
            delivery_generation=int(inflight["delivery_generation"]),
            logical_clock_ms=7000,
        )
        second = acknowledge(
            LedgerStore(db),
            event_seq=int(inflight["event_seq"]),
            delivery_generation=int(inflight["delivery_generation"]),
            logical_clock_ms=7000,
        )
        self.assertTrue(first["ok"])
        self.assertFalse(first["idempotent"])
        self.assertTrue(second["idempotent"])

    def test_expected_version_mismatch_is_rejected_without_write(self) -> None:
        db = str(Path(self.tempdir.name) / "version-check.sqlite3")
        prepare_merged_db(db, self.paths)
        before = json.loads(cli("--db", db, "status").stdout)
        result = cli(
            *replay_args(db=db),
            "--expected-version",
            str(before["ledger_version"] + 10),
        )
        payload = json.loads(result.stdout)
        self.assertEqual(result.returncode, 3)
        self.assertEqual(payload["error"]["code"], "LEDGER_VERSION_REGRESSION")
        after = json.loads(cli("--db", db, "status").stdout)
        self.assertEqual(before["ledger_version"], after["ledger_version"])
        self.assertEqual(before["ledger_sha256"], after["ledger_sha256"])

    def test_semantic_generation_rollback_has_its_own_code(self) -> None:
        db = str(Path(self.tempdir.name) / "semantic-rollback.sqlite3")
        prepare_merged_db(db, self.paths)
        cli(
            "--db",
            db,
            "patch",
            "--file",
            self.paths["patch"],
            check=True,
        )
        before = json.loads(cli("--db", db, "status").stdout)
        result = cli(*merge_args(self.paths, db=db))
        payload = json.loads(result.stdout)
        self.assertEqual(result.returncode, 5)
        self.assertEqual(
            payload["error"]["code"],
            "SEMANTIC_GENERATION_REGRESSION",
        )
        after = json.loads(cli("--db", db, "status").stdout)
        self.assertEqual(before["ledger_version"], after["ledger_version"])


if __name__ == "__main__":
    unittest.main()
