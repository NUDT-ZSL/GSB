from __future__ import annotations

import tempfile
import threading
import unittest
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

from rope_arbiter.cache import ContentAddressedCache
from rope_arbiter.errors import DomainError
from rope_arbiter.models import (
    SnapshotStage,
    SnapshotStore,
    VariantSnapshot,
)
from rope_arbiter.sample import build_sample_cases


class CacheTests(unittest.TestCase):
    def test_content_addressed_reuse_and_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cache = ContentAddressedCache(temporary, max_entries=10)
            components = {
                "schema": "test-v1",
                "node": "angles",
                "variant": "rope",
                "base_theta": 10000.0,
                "upstream_hashes": {},
            }

            def compute():
                return {"kind": "angles"}, {"angles": np.arange(6.0).reshape(3, 2)}

            first = cache.get_or_compute(
                case_id="c1",
                variant="rope",
                node="angles",
                key_components=components,
                snapshot_version=1,
                compute=compute,
            )
            second = cache.get_or_compute(
                case_id="c1",
                variant="rope",
                node="angles",
                key_components=components,
                snapshot_version=99,
                compute=lambda: (_ for _ in ()).throw(AssertionError("must reuse")),
            )
            self.assertEqual(first.status, "recomputed")
            self.assertEqual(second.status, "reused")
            self.assertEqual(first.key, second.key)
            self.assertEqual(first.artifact_hash, second.artifact_hash)
            self.assertTrue(Path(first.artifact_path).exists())
            diagnostic = cache.diagnose("c1", "rope", "angles")
            self.assertEqual(diagnostic["count"], 1)
            self.assertEqual(
                [event["status"] for event in diagnostic["access_history"]],
                ["recomputed", "reused"],
            )
            self.assertEqual(
                diagnostic["access_history"][-1]["snapshot_version"], 99
            )

    def test_cache_entry_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cache = ContentAddressedCache(temporary, max_entries=1)
            for index in range(2):
                with self.subTest(index=index):
                    try:
                        cache.get_or_compute(
                            case_id="c1",
                            variant="rope",
                            node=f"node-{index}",
                            key_components={"index": index},
                            snapshot_version=1,
                            compute=lambda index=index: (
                                {"index": index},
                                {"value": np.asarray([float(index)])},
                            ),
                        )
                    except DomainError as exc:
                        self.assertEqual(exc.code, "E_RESOURCE_LIMIT")
                        self.assertEqual(exc.context["limit"], 1)
                    else:
                        self.assertEqual(index, 0)


class SnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cases = build_sample_cases()
        self.snapshot1 = VariantSnapshot.from_cases(self.cases, 1)
        self.snapshot2 = VariantSnapshot.with_overrides(
            self.snapshot1,
            2,
            {
                self.cases[0].case_id: {
                    "scale_ratio": self.cases[0].scale_ratio * 2.0
                }
            },
        )

    def test_atomic_store_reload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = SnapshotStore(temporary)
            stage = SnapshotStage(store=store)
            receipt = stage.try_commit(self.snapshot1)
            self.assertEqual(receipt.status, "committed")
            reloaded_stage = SnapshotStage(store=SnapshotStore(temporary))
            self.assertEqual(
                reloaded_stage.snapshot().content_checksum,
                self.snapshot1.content_checksum,
            )
            reloaded_stage.try_commit(self.snapshot2)
            latest = SnapshotStore(temporary).load()
            self.assertEqual(latest.version, 2)
            self.assertEqual(
                latest.content_checksum, self.snapshot2.content_checksum
            )

    def test_rollback_and_corruption(self) -> None:
        stage = SnapshotStage(initial=self.snapshot2)
        with self.assertRaises(DomainError) as rollback:
            stage.try_commit(self.snapshot1)
        self.assertEqual(rollback.exception.code, "E_SNAPSHOT_ROLLBACK")

        corrupted = self.snapshot2.to_dict()
        first_case = sorted(corrupted["cases"])[0]
        corrupted["cases"][first_case]["base_theta"] *= 2.0
        with self.assertRaises(DomainError) as corrupt:
            VariantSnapshot.from_dict(corrupted)
        self.assertEqual(corrupt.exception.code, "E_SNAPSHOT_CORRUPT")
        self.assertEqual(corrupt.exception.context["case_id"], first_case)
        self.assertIn("variant", corrupt.exception.context)

        same_version = VariantSnapshot.with_overrides(
            self.snapshot2,
            2,
            {self.cases[1].case_id: {"block_size": 7}},
        )
        with self.assertRaises(DomainError) as conflict:
            stage.try_commit(same_version)
        self.assertEqual(conflict.exception.code, "E_SNAPSHOT_CORRUPT")

    def test_stage_busy_is_immediate_and_non_blocking(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        class BlockingStage(SnapshotStage):
            def _before_publish(self, snapshot):
                entered.set()
                release.wait(timeout=5)

        stage = BlockingStage(initial=self.snapshot1)
        result: dict[str, object] = {}

        def commit_first() -> None:
            try:
                result["receipt"] = stage.try_commit(self.snapshot2)
            except BaseException as exc:  # pragma: no cover - diagnostic path
                result["error"] = exc

        worker = threading.Thread(target=commit_first)
        worker.start()
        self.assertTrue(entered.wait(timeout=5))
        third = VariantSnapshot.with_overrides(
            self.snapshot2,
            3,
            {self.cases[2].case_id: {"freq_block": list(self.cases[2].freq_block)}},
        )
        with self.assertRaises(DomainError) as busy:
            stage.try_commit(third)
        self.assertEqual(busy.exception.code, "E_STAGE_BUSY")
        release.set()
        worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(stage.snapshot().version, 2)
        self.assertNotIn("error", result)

    def test_snapshot_mapping_is_immutable(self) -> None:
        with self.assertRaises(TypeError):
            self.snapshot1.cases["new"] = self.snapshot1.cases[self.cases[0].case_id]  # type: ignore[index]

    @unittest.skipUnless(os.name == "posix", "SIGKILL durability test is POSIX-only")
    def test_force_termination_keeps_last_committed_snapshot_and_cache(self) -> None:
        repository_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ready = root / "ready.json"
            environment = dict(os.environ)
            environment["PYTHONPATH"] = str(repository_root)
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(repository_root / "tests" / "durability_child.py"),
                    str(root),
                    str(ready),
                ],
                cwd=repository_root,
                env=environment,
            )
            try:
                deadline = time.monotonic() + 15
                while not ready.exists() and time.monotonic() < deadline:
                    if process.poll() is not None:
                        self.fail(f"durability child exited early: {process.returncode}")
                    time.sleep(0.05)
                self.assertTrue(ready.exists(), "durability child did not reach ready state")
                child_state = json.loads(ready.read_text(encoding="utf-8"))
                process.kill()
                process.wait(timeout=10)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=10)

            loaded_snapshot = SnapshotStore(root / "snapshot_stage").load()
            self.assertIsNotNone(loaded_snapshot)
            self.assertEqual(loaded_snapshot.version, 2)
            self.assertEqual(
                loaded_snapshot.content_checksum,
                child_state["snapshot_checksum"],
            )
            cache = ContentAddressedCache(root / "cache", max_entries=100)
            diagnostic = cache.diagnose("durable-case", "rope", "angles")
            self.assertEqual(diagnostic["count"], 1)
            self.assertEqual(
                diagnostic["entries"][0]["artifact_hash"],
                child_state["artifact_hash"],
            )
            reused = cache.get_or_compute(
                case_id="durable-case",
                variant="rope",
                node="angles",
                key_components={"durable": True, "node": "angles"},
                snapshot_version=99,
                compute=lambda: (_ for _ in ()).throw(
                    AssertionError("durable cache entry must be reused")
                ),
            )
            self.assertEqual(reused.status, "reused")
            self.assertEqual(reused.artifact_hash, child_state["artifact_hash"])


if __name__ == "__main__":
    unittest.main()
