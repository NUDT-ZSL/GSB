from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from seqledger.errors import InputError
from seqledger.sample import generate_fixtures
from seqledger.util import read_json
from seqledger.validation import (
    validate_events,
    validate_patch,
    validate_topology,
    validate_v1,
    validate_v2,
)

from .helpers import cli, fixture_paths


class ValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tempdir = tempfile.TemporaryDirectory()
        cls.fixture_dir = Path(cls.tempdir.name) / "fixtures"
        generate_fixtures(cls.fixture_dir)
        cls.paths = fixture_paths(cls.fixture_dir)
        cls.topology = validate_topology(
            read_json(cls.paths["topology"]),
            source=cls.paths["topology"],
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tempdir.cleanup()

    def test_event_out_of_order_has_distinct_code_and_no_ledger(self) -> None:
        events = [
            json.loads(line)
            for line in Path(self.paths["events"]).read_text(encoding="utf-8").splitlines()
        ]
        events[1]["event_seq"] = 999
        bad_events = Path(self.tempdir.name) / "out-of-order.jsonl"
        bad_events.write_text(
            "".join(json.dumps(event) + "\n" for event in events),
            encoding="utf-8",
        )
        db = str(Path(self.tempdir.name) / "invalid-order.sqlite3")
        result = cli(
            "--db",
            db,
            "load",
            "--topology",
            self.paths["topology"],
            "--events",
            str(bad_events),
            "--subscriptions",
            self.paths["subscriptions"],
        )
        payload = json.loads(result.stdout)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(payload["error"]["code"], "EVENT_OUT_OF_ORDER")
        self.assertFalse(Path(db).exists())

    def test_missing_partition_has_distinct_code(self) -> None:
        events = [
            json.loads(line)
            for line in Path(self.paths["events"]).read_text(encoding="utf-8").splitlines()
        ]
        events[0]["topic"] = "not-declared"
        with self.assertRaises(InputError) as caught:
            validate_events(events, self.topology, source="<bad-events>")
        self.assertEqual(caught.exception.code, "PARTITION_MISSING")

    def test_duplicate_event_sequence(self) -> None:
        events = [
            json.loads(line)
            for line in Path(self.paths["events"]).read_text(encoding="utf-8").splitlines()
        ]
        events[1]["event_seq"] = events[0]["event_seq"]
        with self.assertRaises(InputError) as caught:
            validate_events(events, self.topology, source="<duplicate-events>")
        self.assertEqual(caught.exception.code, "EVENT_DUPLICATE_SEQUENCE")

    def test_v1_relative_seconds_negative(self) -> None:
        raw = read_json(self.paths["v1"])
        raw["entries"][0]["relative_effective_seconds"] = -1
        with self.assertRaises(InputError) as caught:
            validate_v1(raw)
        self.assertEqual(caught.exception.code, "V1_RELATIVE_SECONDS_INVALID")

    def test_v2_generation_regression(self) -> None:
        manifest = read_json(self.paths["v2_manifest"])
        manifest["shards"][1]["semantic_generation"] = 1
        with self.assertRaises(InputError) as caught:
            validate_v2(
                manifest,
                self.paths["v2_manifest"],
                meta_path=self.paths["v2_meta"],
            )
        self.assertEqual(caught.exception.code, "V2_GENERATION_REGRESSION")

    def test_v2_shard_hash_mismatch(self) -> None:
        temp = Path(self.tempdir.name) / "hash-check"
        if temp.exists():
            shutil.rmtree(temp)
        shutil.copytree(Path(self.paths["v2_manifest"]).parent, temp)
        shard = temp / "shard-000.json"
        shard.write_text(shard.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        with self.assertRaises(InputError) as caught:
            validate_v2(
                read_json(temp / "manifest.json"),
                str(temp / "manifest.json"),
                meta_path=str(temp / "meta.sqlite3"),
            )
        self.assertEqual(caught.exception.code, "V2_SHARD_HASH_MISMATCH")

    def test_v2_tombstone_reference_must_close(self) -> None:
        temp = Path(self.tempdir.name) / "tombstone-check"
        if temp.exists():
            shutil.rmtree(temp)
        shutil.copytree(Path(self.paths["v2_manifest"]).parent, temp)
        connection = sqlite3.connect(str(temp / "meta.sqlite3"))
        try:
            connection.execute("DELETE FROM tombstones")
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(InputError) as caught:
            validate_v2(
                read_json(temp / "manifest.json"),
                str(temp / "manifest.json"),
                meta_path=str(temp / "meta.sqlite3"),
            )
        self.assertEqual(caught.exception.code, "V2_TOMBSTONE_OPEN")

    def test_patch_generation_regression(self) -> None:
        raw = read_json(self.paths["patch"])
        raw["semantic_generation"] = 3
        raw["updates"][0]["semantic_generation"] = 3
        raw["tombstones"][0]["semantic_generation"] = 3
        with self.assertRaises(InputError) as caught:
            validate_patch(raw, current_v2_generation=4)
        self.assertEqual(caught.exception.code, "PATCH_GENERATION_REGRESSION")

    def test_invalid_merge_does_not_change_ledger_version(self) -> None:
        from .helpers import load_args

        db = str(Path(self.tempdir.name) / "invalid-merge.sqlite3")
        cli(*load_args(self.paths, db=db), check=True)
        before = json.loads(
            cli("--db", db, "status").stdout
        )["ledger_version"]
        bad_v1 = Path(self.tempdir.name) / "bad-v1.json"
        raw = read_json(self.paths["v1"])
        raw["entries"][0]["relative_effective_seconds"] = -1
        bad_v1.write_text(json.dumps(raw), encoding="utf-8")
        result = cli(
            "--db",
            db,
            "merge",
            "--v1",
            str(bad_v1),
            "--v2-manifest",
            self.paths["v2_manifest"],
            "--v2-meta",
            self.paths["v2_meta"],
            "--logical-clock-ms",
            "3000",
        )
        payload = json.loads(result.stdout)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(payload["error"]["code"], "V1_RELATIVE_SECONDS_INVALID")
        after = json.loads(cli("--db", db, "status").stdout)["ledger_version"]
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
