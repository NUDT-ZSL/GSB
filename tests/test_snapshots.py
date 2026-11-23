from __future__ import annotations

import json
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from support_summary.errors import BusinessError, InjectedCrash
from support_summary.canonical import sha256_value
from support_summary.pipeline import load_runner
from support_summary.snapshots import SnapshotManager
from support_summary.validation import (
    dataset_signature,
    load_config,
    load_sessions,
    load_vocabulary,
)

from .helpers import make_dataset


class SnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.paths = make_dataset(self.root)
        self.sessions = load_sessions(self.paths["sessions"])
        self.vocabulary = load_vocabulary(self.paths["vocabulary"])
        self.config = load_config(self.paths["config"])
        self.signature = dataset_signature(
            self.sessions, self.vocabulary, self.config
        )
        self.session_ids = [session.session_id for session in self.sessions]

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _manager(self, name: str = "checkpoint") -> SnapshotManager:
        return SnapshotManager(
            self.root / name, self.session_ids, self.signature
        )

    def test_recovery_from_last_committed_stage(self) -> None:
        checkpoint = self.root / "checkpoint-recovery"
        cache = self.root / "cache-recovery"
        manager = self._manager("checkpoint-recovery")
        manager.reset()
        runner = load_runner(
            self.paths["sessions"],
            self.paths["vocabulary"],
            self.paths["config"],
            cache,
        )
        with self.assertRaises(InjectedCrash):
            runner.run(
                checkpoint_dir=checkpoint,
                resume=False,
                crash_at="rewrite:partial",
            )
        snapshots = manager.load()
        self.assertEqual(
            list(snapshots), ["chunk_tokenize", "unit_score"]
        )

        resumed = load_runner(
            self.paths["sessions"],
            self.paths["vocabulary"],
            self.paths["config"],
            cache,
        ).run(checkpoint_dir=checkpoint, resume=True)
        baseline = load_runner(
            self.paths["sessions"],
            self.paths["vocabulary"],
            self.paths["config"],
            self.root / "cache-baseline",
        ).run()
        self.assertEqual(resumed.summary_bytes, baseline.summary_bytes)
        self.assertEqual(
            resumed.resumed_stages, ["chunk_tokenize", "unit_score"]
        )
        self.assertEqual(
            resumed.recomputed_stages, ["rewrite", "summary_merge"]
        )
        rewrite_events = [
            event for event in resumed.events if event["stage"] == "rewrite"
        ]
        self.assertTrue(rewrite_events)
        self.assertTrue(
            all(event["status"] == "recomputed" for event in rewrite_events)
        )

    def test_rollback_and_whole_replacement(self) -> None:
        manager = self._manager("checkpoint-rollback")
        first = manager.commit(
            "chunk_tokenize", {"value": "first"}, self.session_ids
        )
        self.assertEqual(first["version"], 1)
        replacement = manager.commit(
            "chunk_tokenize",
            {"value": "replacement"},
            self.session_ids,
            version=2,
        )
        self.assertEqual(replacement["version"], 2)
        self.assertEqual(manager.load()["chunk_tokenize"]["data"]["value"], "replacement")
        with self.assertRaises(BusinessError) as caught:
            manager.commit(
                "chunk_tokenize",
                {"value": "old"},
                self.session_ids,
                version=1,
            )
        self.assertEqual(caught.exception.code, "E_SNAPSHOT_ROLLBACK")
        self.assertEqual(caught.exception.version, 1)

    def test_corrupt_checksum_reports_stage_and_version(self) -> None:
        manager = self._manager("checkpoint-corrupt")
        envelope = manager.commit(
            "chunk_tokenize", {"value": "ok"}, self.session_ids
        )
        path = manager.root / "01_chunk_tokenize.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        value["checksum"] = "0" * 64
        path.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaises(BusinessError) as caught:
            manager.load()
        self.assertEqual(caught.exception.code, "E_SNAPSHOT_CORRUPT")
        self.assertEqual(caught.exception.stage, "chunk_tokenize")
        self.assertEqual(caught.exception.version, envelope["version"])

    def test_version_regression_on_load_is_rollback(self) -> None:
        manager = self._manager("checkpoint-version-regression")
        manager.commit(
            "chunk_tokenize", {"value": 1}, self.session_ids, version=1
        )
        manager.commit(
            "unit_score", {"value": 2}, self.session_ids, version=2
        )
        path = manager.root / "02_unit_score.json"
        envelope = json.loads(path.read_text(encoding="utf-8"))
        envelope["version"] = 1
        base = {
            key: value
            for key, value in envelope.items()
            if key != "checksum"
        }
        envelope["checksum"] = sha256_value(base)
        path.write_text(json.dumps(envelope), encoding="utf-8")
        with self.assertRaises(BusinessError) as caught:
            manager.load()
        self.assertEqual(caught.exception.code, "E_SNAPSHOT_ROLLBACK")
        self.assertEqual(caught.exception.stage, "unit_score")
        self.assertEqual(caught.exception.version, 1)

    def test_concurrent_commit_is_immediately_busy(self) -> None:
        manager = self._manager("checkpoint-busy")
        started = time.monotonic()
        with manager.stage_lock("chunk_tokenize"):
            with self.assertRaises(BusinessError) as caught:
                manager.commit(
                    "chunk_tokenize", {"value": 1}, self.session_ids
                )
        elapsed = time.monotonic() - started
        self.assertEqual(caught.exception.code, "E_STAGE_BUSY")
        self.assertLess(elapsed, 1.0)


if __name__ == "__main__":
    unittest.main()
