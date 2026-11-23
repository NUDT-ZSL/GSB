from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from walcp.checkpoints import merge_checkpoints
from walcp.engine import AppService
from walcp.errors import AppError, ErrorCode
from walcp.models import CheckpointVersion, SnapshotRow
from walcp.util import absolute_ms_from_relative_seconds, stable_json

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"
NOW = 1_700_000_500_000
ANCHOR = 1_700_000_100_000


class CoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.work = Path(self.temp.name)
        self.db = self.work / "state.db"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _load(self, db: Path | None = None) -> dict:
        service = AppService(db or self.db)
        return service.load(SAMPLES, now_ms=NOW, anchor_ms=ANCHOR)

    def _apply(self, now_ms: int = NOW, db: Path | None = None) -> dict:
        return AppService(db or self.db).apply(now_ms=now_ms)

    def test_load_merge_incremental_and_full_replay_match(self) -> None:
        loaded = self._load()
        self.assertEqual(loaded["watermark"], 14)
        self.assertEqual(loaded["checkpoint_generation"], 2)
        self.assertEqual(loaded["checkpoint_source_format"], "v2")
        generation_1 = next(
            item
            for item in loaded["merge"]["generations"]
            if item["generation"] == 1
        )
        self.assertEqual(generation_1["source_format"], "v2")
        v1_source = next(
            item
            for item in loaded["merge"]["source_versions"]
            if item["source_format"] == "v1"
        )
        self.assertFalse(v1_source["selected"])
        conversion = v1_source["v1_conversion"]
        self.assertEqual(conversion["scale_ms_per_unit"], 1000)
        self.assertEqual(conversion["rounding"], "half_up")
        self.assertEqual(conversion["raw_relative_seconds"], 20.0)

        applied = self._apply()
        self.assertEqual(applied["applied_txns"], list(range(15, 23)))
        self.assertEqual(applied["watermark"], 22)
        verified = AppService(self.db).verify()
        self.assertTrue(verified["ok"])
        self.assertTrue(verified["matched"])
        self.assertIsNone(verified["first_difference"])
        self.assertEqual(verified["row_count"], verified["actual_row_count"])

    def test_apply_is_idempotent_and_status_is_stable(self) -> None:
        self._load()
        first = self._apply()
        self.assertEqual(len(first["applied_txns"]), 8)
        status_first = AppService(self.db).status()
        second = self._apply()
        self.assertEqual(second["applied_txns"], [])
        status_second = AppService(self.db).status()
        self.assertEqual(
            status_first["delay_summary"], status_second["delay_summary"]
        )
        self.assertEqual(status_first["applied_transactions"], 22)
        self.assertEqual(
            stable_json(status_first), stable_json(AppService(self.db).status())
        )

    def test_clock_rollback_uses_independent_error_code(self) -> None:
        self._load()
        self._apply()
        with self.assertRaises(AppError) as context:
            AppService(self.db).apply(now_ms=NOW - 1)
        self.assertEqual(context.exception.code, ErrorCode.CLOCK_ROLLBACK)
        tolerant = AppService(self.db).apply(now_ms=NOW - 1, tolerance_ms=5)
        self.assertTrue(tolerant["ok"])

    def test_edit_closure_reuses_unaffected_delays(self) -> None:
        self._load()
        self._apply()
        before = AppService(self.db).status()
        edited = AppService(self.db).edit(
            18,
            changes={"after": {"balance": 32}},
            now_ms=NOW + 100_000,
        )
        row_versions = edited["closure"]["row_versions"]
        self.assertEqual(
            {(item["txn_id"], item["version"]) for item in row_versions},
            {(18, 2), (22, 3)},
        )
        self.assertEqual(edited["closure"]["checkpoint_refs"], [])
        self.assertEqual(
            {item["txn_id"] for item in edited["safe_reuse"]}, {18, 22}
        )
        self.assertTrue(edited["reuse_denials"])
        stale = AppService(self.db).status()
        self.assertTrue(stale["needs_rebuild"])
        self.assertEqual(before["delay_summary"], stale["delay_summary"])

        applied = AppService(self.db).apply(now_ms=NOW + 100_000)
        self.assertTrue(applied["rebuilt"])
        self.assertEqual(set(applied["reused_delays"]), set(range(15, 23)))
        after = AppService(self.db).status()
        self.assertEqual(before["delay_summary"], after["delay_summary"])
        self.assertTrue(AppService(self.db).verify()["matched"])

    def test_edit_before_current_checkpoint_invalidates_checkpoint_chain(self) -> None:
        self._load()
        self._apply()
        before_delays = AppService(self.db).status()["delay_summary"]["records"]
        edited = AppService(self.db).edit(
            12,
            changes={"after": {"balance": 76}},
            now_ms=NOW + 100_000,
        )
        refs = edited["closure"]["checkpoint_refs"]
        self.assertEqual([item["generation"] for item in refs], [2])
        self.assertEqual(edited["rebuild"]["baseline_generation"], 1)
        stale = AppService(self.db).status()
        self.assertEqual(stale["checkpoint_generation"], 1)
        self.assertEqual(stale["watermark"], 8)
        self.assertTrue(stale["needs_rebuild"])
        applied = AppService(self.db).apply(now_ms=NOW + 100_000)
        self.assertEqual(applied["applied_txns"][0], 9)
        self.assertTrue(set(range(15, 23)).issubset(applied["reused_delays"]))
        after_delays = AppService(self.db).status()["delay_summary"]["records"]
        before_reused = [
            record for record in before_delays if record["txn_id"] >= 15
        ]
        after_reused = [
            record for record in after_delays if record["txn_id"] >= 15
        ]
        self.assertEqual(before_reused, after_reused)
        self.assertTrue(AppService(self.db).verify()["matched"])

    def test_checkpoint_create_receive_noop_and_rollback(self) -> None:
        self._load()
        self._apply()
        package = self.work / "gen3"
        created = AppService(self.db).checkpoint(
            out_dir=package, now_ms=NOW + 200_000
        )
        self.assertEqual(created["decision"], "created")
        self.assertEqual(created["generation"], 3)
        self.assertEqual(created["interval_ms"], 560_000)
        listed = AppService(self.db).checkpoint_list()
        self.assertEqual(listed["pointer_generation"], 3)
        noop = AppService(self.db).checkpoint(
            package=package, now_ms=NOW + 200_000
        )
        self.assertEqual(noop["decision"], "noop")
        with self.assertRaises(AppError) as context:
            AppService(self.db).checkpoint(
                package=SAMPLES / "checkpoints" / "v2",
                now_ms=NOW + 200_000,
            )
        self.assertEqual(context.exception.code, ErrorCode.CHECKPOINT_ROLLBACK)
        self.assertTrue(AppService(self.db).verify()["matched"])

    def test_incremental_and_full_replacement_receive(self) -> None:
        source_dir = self.work / "source"
        target_dir = self.work / "target"
        source_db = source_dir / "state.db"
        target_db = target_dir / "state.db"
        source_dir.mkdir()
        target_dir.mkdir()
        for db in (source_db, target_db):
            self._load(db)
            self._apply(db=db)
        package = source_dir / "gen3"
        AppService(source_db).checkpoint(
            out_dir=package, now_ms=NOW + 200_000
        )
        incremental = AppService(target_db).checkpoint(
            package=package, now_ms=NOW + 200_000
        )
        self.assertEqual(incremental["decision"], "incremental_append")
        self.assertEqual(incremental["generation"], 3)
        full_package = source_dir / "full4"
        AppService(source_db).checkpoint(
            out_dir=full_package,
            now_ms=NOW + 300_000,
            full_replace=True,
        )
        replacement = AppService(target_db).checkpoint(
            package=full_package, now_ms=NOW + 300_000
        )
        self.assertEqual(replacement["decision"], "full_replace")
        self.assertEqual(replacement["generation"], 4)
        self.assertTrue(AppService(target_db).verify()["matched"])

    def test_same_clock_merge_uses_v2_priority(self) -> None:
        snapshot = [
            SnapshotRow(
                table="accounts",
                key="a",
                version=1,
                txn_id=1,
                deleted=False,
                value={"balance": 1},
            )
        ]
        common = dict(
            generation=1,
            source_path="test",
            source_hash="hash",
            saved_at_ms=1000,
            replay_generation=7,
            applied_watermark=1,
            snapshot_rows=snapshot,
            tables={},
        )
        v1 = CheckpointVersion(source_format="v1", **common)
        v2 = CheckpointVersion(source_format="v2", **common)
        merged = merge_checkpoints([v1, v2], anchor_ms=0)
        self.assertIs(merged.baseline, v2)
        self.assertIn(
            "format_priority", merged.baseline.decision_reason["why"]
        )

    def test_v1_relative_seconds_round_half_up(self) -> None:
        absolute, conversion = absolute_ms_from_relative_seconds(0.0005, 100)
        self.assertEqual(absolute, 101)
        self.assertEqual(conversion["rounding"], "half_up")
        self.assertEqual(
            conversion["rounding_direction"],
            "nearest_ties_away_from_zero",
        )

    def test_summary_only_v1_baseline_is_materialized_from_wal_prefix(self) -> None:
        samples = self.work / "v1_samples"
        shutil.copytree(SAMPLES, samples)
        shutil.rmtree(samples / "checkpoints" / "v2")
        v1_path = (
            samples / "checkpoints" / "v1" / "cp-0001-v1.json"
        )
        checkpoint = json.loads(v1_path.read_text(encoding="utf-8"))
        checkpoint.pop("snapshot_rows", None)
        v1_path.write_text(
            json.dumps(checkpoint, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        from walcp.util import canonical_json
        from walcp.wal import compute_content_hash

        for path in sorted((samples / "wal").glob("*.jsonl")):
            records = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
            ]
            for record in records:
                record["checkpoint_gen"] = 1
                record["content_hash"] = compute_content_hash(record)
            path.write_text(
                "\n".join(canonical_json(record) for record in records) + "\n",
                encoding="utf-8",
            )
        loaded = AppService(self.db).load(
            samples, now_ms=NOW, anchor_ms=ANCHOR
        )
        self.assertEqual(loaded["checkpoint_source_format"], "v1")
        self.assertEqual(loaded["watermark"], 8)
        self.assertIsNotNone(loaded["baseline_materialization"])
        self.assertGreater(loaded["baseline_materialization"]["rows"], 0)
        applied = AppService(self.db).apply(now_ms=NOW)
        self.assertEqual(applied["applied_txns"][0], 9)
        self.assertTrue(AppService(self.db).verify()["matched"])


if __name__ == "__main__":
    unittest.main()
