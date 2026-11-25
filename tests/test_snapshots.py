from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

from term_pipeline.errors import (
    E_SNAPSHOT_CORRUPT,
    E_SNAPSHOT_ROLLBACK,
    PipelineError,
)
from term_pipeline.snapshots import SnapshotStore
from tests.helpers import new_workspace, run_workspace


class SnapshotTests(unittest.TestCase):
    def test_atomic_replacement_and_rollback(self) -> None:
        temp, root = new_workspace()
        self.addCleanup(temp.cleanup)
        store = SnapshotStore(root / "snapshots")
        first = store.commit(
            0,
            {"documents": [{"doc_id": "doc-1"}]},
            ["doc-1"],
            base_fingerprint="base",
            expected_doc_ids=["doc-1"],
        )
        second = store.commit(
            0,
            {"documents": [{"doc_id": "doc-1", "text": "replacement"}]},
            ["doc-1"],
            base_fingerprint="base",
            expected_doc_ids=["doc-1"],
        )
        self.assertGreater(second["version"], first["version"])
        self.assertEqual(len(list((root / "snapshots").glob("*.json"))), 1)
        self.assertEqual(
            store.load_all()[0]["payload"]["documents"][0]["text"],
            "replacement",
        )
        with self.assertRaises(PipelineError) as caught:
            store.commit(
                0,
                {"documents": []},
                [],
                base_fingerprint="base",
                forced_version=1,
            )
        self.assertEqual(caught.exception.code, E_SNAPSHOT_ROLLBACK)

    def test_incomplete_stage_cannot_commit(self) -> None:
        temp, root = new_workspace()
        self.addCleanup(temp.cleanup)
        store = SnapshotStore(root / "snapshots")
        with self.assertRaises(PipelineError):
            store.commit(
                0,
                {"documents": [{"doc_id": "doc-1"}]},
                ["doc-1"],
                base_fingerprint="base",
                expected_doc_ids=["doc-1", "doc-2"],
            )
        self.assertEqual(store.load_all(), [])

    def test_corruption_is_detected(self) -> None:
        temp, root = new_workspace()
        self.addCleanup(temp.cleanup)
        run_workspace(root)
        store = SnapshotStore(root / "snapshots")
        path = store.path_for(1)
        value = json.loads(path.read_text(encoding="utf-8"))
        value["payload"]["documents"][0]["candidates"][0]["term"] = "BROKEN"
        path.write_text(
            json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        with self.assertRaises(PipelineError) as caught:
            store.load_all()
        self.assertEqual(caught.exception.code, E_SNAPSHOT_CORRUPT)

    def test_sigkill_recovery_matches_continuous_run(self) -> None:
        baseline_temp, baseline = new_workspace()
        self.addCleanup(baseline_temp.cleanup)
        run_workspace(baseline)
        expected = (baseline / "out" / "term_index.json").read_bytes()

        for stage in (1, 2, 3, 4, 5):
            with self.subTest(stage=stage):
                temp, root = new_workspace()
                self.addCleanup(temp.cleanup)
                killed = subprocess.run(
                    [
                        sys.executable,
                        "main.py",
                        "run",
                        "--config",
                        str(root / "config.json"),
                        "--crash-after-stage",
                        str(stage),
                    ],
                    cwd=Path(__file__).resolve().parents[1],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertNotEqual(
                    killed.returncode,
                    0,
                    killed.stdout + killed.stderr,
                )
                resumed = subprocess.run(
                    [
                        sys.executable,
                        "main.py",
                        "run",
                        "--config",
                        str(root / "config.json"),
                    ],
                    cwd=Path(__file__).resolve().parents[1],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(
                    resumed.returncode,
                    0,
                    resumed.stdout + resumed.stderr,
                )
                self.assertEqual(
                    (root / "out" / "term_index.json").read_bytes(),
                    expected,
                )
                store = SnapshotStore(root / "snapshots")
                self.assertEqual(
                    [item["version"] for item in store.load_all()],
                    [1, 2, 3, 4, 5],
                )

