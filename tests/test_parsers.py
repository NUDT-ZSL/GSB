from __future__ import annotations

import hashlib
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from cacheman.errors import (
    CACHE_ARTIFACT_MISSING,
    CACHE_DIGEST_MISMATCH,
    CACHE_GENERATION_REGRESSION,
    CACHE_MANIFEST_INVALID,
    CACHE_SHARD_HASH_MISMATCH,
    CACHE_TOMBSTONE_INVALID,
    CacheError,
)
from cacheman.v1 import parse_v1
from cacheman.v2 import parse_v2

from .helpers import ROOT, write_json


class V1ParserTests(unittest.TestCase):
    def test_sample_v1_is_valid(self) -> None:
        dataset = parse_v1(ROOT / "samples/v1/cache.json")
        self.assertEqual(len(dataset.candidates), 3)
        by_key = {(item.task_key, item.artifact): item for item in dataset.candidates}
        lint = by_key[("legacy/lint", "lint.txt")]
        self.assertEqual(lint.absolute_written_ms, 1_760_000_100_000 - 120_000)
        self.assertEqual(lint.original_time["scale_ms_per_second"], 1000)
        self.assertEqual(lint.original_time["rounding"], "floor")
        injected = parse_v1(
            ROOT / "samples/v1/cache.json",
            time_reference_ms=1_760_000_200_000,
        )
        injected_lint = next(
            item
            for item in injected.candidates
            if item.task_key == "legacy/lint"
        )
        self.assertEqual(injected_lint.absolute_written_ms, 1_760_000_080_000)
        self.assertEqual(
            injected_lint.original_time["manifest_logical_clock_ms"],
            1_760_000_100_000,
        )

    def test_missing_artifact_returns_business_code(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_json(
                root / "cache.json",
                {
                    "format": "cache-v1",
                    "manifest_id": "missing",
                    "manifest_version": 1,
                    "logical_clock_ms": 1000,
                    "artifacts": [
                        {
                            "task": "build",
                            "artifact": "app.tar",
                            "sha256": "sha256:" + "0" * 64,
                            "relative_written_seconds": 1,
                            "size_bytes": 1,
                            "content_path": "missing.bin",
                        }
                    ],
                    "task_artifacts": {"build": ["app.tar"]},
                },
            )
            with self.assertRaises(CacheError) as caught:
                parse_v1(root / "cache.json")
            self.assertEqual(caught.exception.code, CACHE_ARTIFACT_MISSING)

    def test_digest_mismatch_returns_business_code(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "app.bin").write_bytes(b"actual")
            write_json(
                root / "cache.json",
                {
                    "format": "cache-v1",
                    "manifest_id": "bad-digest",
                    "manifest_version": 1,
                    "logical_clock_ms": 1000,
                    "artifacts": [
                        {
                            "task": "build",
                            "artifact": "app.tar",
                            "sha256": "sha256:" + "1" * 64,
                            "relative_written_seconds": 1,
                            "size_bytes": 6,
                            "content_path": "app.bin",
                        }
                    ],
                    "task_artifacts": {"build": ["app.tar"]},
                },
            )
            with self.assertRaises(CacheError) as caught:
                parse_v1(root / "cache.json")
            self.assertEqual(caught.exception.code, CACHE_DIGEST_MISMATCH)
            self.assertIn("actual_sha256", caught.exception.fields)

    def test_flat_map_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "app.bin").write_bytes(b"actual")
            write_json(
                root / "cache.json",
                {
                    "format": "cache-v1",
                    "manifest_id": "bad-map",
                    "manifest_version": 1,
                    "logical_clock_ms": 1000,
                    "artifacts": [
                        {
                            "task": "build",
                            "artifact": "app.tar",
                            "sha256": "sha256:"
                            + hashlib.sha256(b"actual").hexdigest(),
                            "relative_written_seconds": 1,
                            "size_bytes": 6,
                            "content_path": "app.bin",
                        }
                    ],
                    "task_artifacts": {},
                },
            )
            with self.assertRaises(CacheError) as caught:
                parse_v1(root / "cache.json")
            self.assertEqual(caught.exception.code, CACHE_MANIFEST_INVALID)
            self.assertEqual(caught.exception.fields["missing_tasks"], ["build"])


class V2ParserTests(unittest.TestCase):
    def _copy_v2_fixture(self, root: Path) -> Path:
        shutil.copytree(ROOT / "samples/v2", root / "v2")
        shutil.copytree(ROOT / "samples/artifacts/v2", root / "artifacts/v2")
        return root / "v2/meta.sqlite3"

    def test_sample_v2_is_valid(self) -> None:
        dataset = parse_v2(ROOT / "samples/v2/meta.sqlite3")
        self.assertEqual(len(dataset.candidates), 10)
        self.assertEqual(len(dataset.tombstones), 1)
        source = dataset.sources[0]
        self.assertEqual((source.generation_min, source.generation_max), (6, 7))
        self.assertEqual(len(source.shard_hashes), 2)

    def test_shard_hash_mismatch_returns_business_code(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            db_path = self._copy_v2_fixture(Path(temp))
            shard_path = Path(temp) / "v2/shards/shard-0006.json"
            shard_path.write_bytes(shard_path.read_bytes() + b" ")
            with self.assertRaises(CacheError) as caught:
                parse_v2(db_path)
            self.assertEqual(caught.exception.code, CACHE_SHARD_HASH_MISMATCH)

    def test_invalid_tombstone_target_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            db_path = self._copy_v2_fixture(Path(temp))
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    "UPDATE v2_tombstones SET digest = ?",
                    ("sha256:" + "0" * 64,),
                )
                conn.commit()
            finally:
                conn.close()
            with self.assertRaises(CacheError) as caught:
                parse_v2(db_path)
            self.assertEqual(caught.exception.code, CACHE_TOMBSTONE_INVALID)

    def test_non_integer_absolute_timestamp_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            db_path = self._copy_v2_fixture(Path(temp))
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    "UPDATE v2_artifacts SET absolute_written_ms = 'not-an-int' "
                    "WHERE task_key = 'app/build'"
                )
                conn.commit()
            finally:
                conn.close()
            with self.assertRaises(CacheError) as caught:
                parse_v2(db_path)
            self.assertEqual(caught.exception.code, CACHE_MANIFEST_INVALID)

    def test_generation_below_declared_floor_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            db_path = self._copy_v2_fixture(Path(temp))
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    "UPDATE v2_meta SET value = '8' "
                    "WHERE key = 'cache_generation_floor'"
                )
                conn.commit()
            finally:
                conn.close()
            with self.assertRaises(CacheError) as caught:
                parse_v2(db_path)
            self.assertEqual(caught.exception.code, CACHE_GENERATION_REGRESSION)


if __name__ == "__main__":
    unittest.main()
