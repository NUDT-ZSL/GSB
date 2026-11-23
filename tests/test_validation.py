from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from walcp.engine import AppService
from walcp.errors import AppError, ErrorCode
from walcp.util import canonical_json
from walcp.wal import compute_content_hash

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"
NOW = 1_700_000_500_000


class ValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.work = Path(self.temp.name)
        self.samples = self.work / "samples"
        shutil.copytree(SAMPLES, self.samples)
        self.db = self.work / "must_not_exist.db"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _load_error(self) -> AppError:
        with self.assertRaises(AppError) as context:
            AppService(self.db).load(
                self.samples, now_ms=NOW, anchor_ms=1_700_000_100_000
            )
        self.assertFalse(self.db.exists())
        return context.exception

    def _rewrite_first_line(
        self, segment: str, mutation, *, recompute_hash: bool = True
    ) -> None:
        path = self.samples / "wal" / segment
        lines = path.read_text(encoding="utf-8").splitlines()
        record = json.loads(lines[0])
        mutation(record)
        if recompute_hash and "content_hash" in record:
            try:
                record["content_hash"] = compute_content_hash(record)
            except KeyError:
                pass
        lines[0] = canonical_json(record)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_invalid_op_has_distinct_error_code(self) -> None:
        self._rewrite_first_line(
            "wal-000001.jsonl", lambda record: record.update(op="upsert")
        )
        self.assertEqual(self._load_error().code, ErrorCode.WAL_INVALID_ROW)

    def test_hash_mismatch_has_distinct_error_code(self) -> None:
        def mutate(record):
            record["after"] = {"balance": 999}

        self._rewrite_first_line(
            "wal-000001.jsonl", mutate, recompute_hash=False
        )
        self.assertEqual(self._load_error().code, ErrorCode.WAL_HASH_MISMATCH)

    def test_cross_segment_chain_break_has_distinct_error_code(self) -> None:
        self._rewrite_first_line(
            "wal-000002.jsonl",
            lambda record: record.update(txn_id=8),
        )
        self.assertEqual(self._load_error().code, ErrorCode.WAL_CHAIN_BROKEN)

    def test_missing_checkpoint_dependency_has_distinct_error_code(self) -> None:
        def mutate(record):
            record["checkpoint_gen"] = 99

        self._rewrite_first_line("wal-000001.jsonl", mutate)
        self.assertEqual(
            self._load_error().code, ErrorCode.WAL_CHECKPOINT_DEPENDENCY
        )

    def test_v2_shard_hash_mismatch_has_distinct_error_code(self) -> None:
        shard = (
            self.samples
            / "checkpoints"
            / "v2"
            / "shards"
            / "gen-0001-accounts.json"
        )
        payload = json.loads(shard.read_text(encoding="utf-8"))
        payload["table_digest"] = "sha256:tampered"
        shard.write_text(canonical_json(payload), encoding="utf-8")
        self.assertEqual(
            self._load_error().code, ErrorCode.CHECKPOINT_HASH_MISMATCH
        )

    def test_v2_replay_generation_monotonicity_is_enforced(self) -> None:
        db_path = self.samples / "checkpoints" / "v2" / "metadata.sqlite"
        connection = sqlite3.connect(db_path)
        try:
            connection.execute(
                "UPDATE checkpoint_meta SET replay_generation = 5 "
                "WHERE generation = 2"
            )
            connection.commit()
        finally:
            connection.close()
        self.assertEqual(
            self._load_error().code, ErrorCode.CHECKPOINT_CHAIN_BROKEN
        )

    def test_edit_invalid_value_does_not_persist(self) -> None:
        db = self.work / "state.db"
        AppService(db).load(
            SAMPLES, now_ms=NOW, anchor_ms=1_700_000_100_000
        )
        before = AppService(db).status()
        with self.assertRaises(AppError) as context:
            AppService(db).edit(
                5,
                changes={"after": None},
                now_ms=NOW + 1000,
            )
        self.assertEqual(context.exception.code, ErrorCode.WAL_INVALID_ROW)
        after = AppService(db).status()
        self.assertEqual(before["edit_count"], after["edit_count"])
        self.assertFalse(after["needs_rebuild"])


if __name__ == "__main__":
    unittest.main()
