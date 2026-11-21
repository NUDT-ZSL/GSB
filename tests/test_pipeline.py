import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from mmpipe.config import BudgetConfig, PipelineConfig
from mmpipe.consistency import verify_snapshots
from mmpipe.errors import InconsistentError, ResourceLimitError, SnapshotCorruptError
from mmpipe.fault_tools import corrupt_snapshot
from mmpipe.manifest import generate_manifest, load_manifest
from mmpipe.pipeline import Pipeline, PipelineExecutionError

REPO_ROOT = Path(__file__).resolve().parents[1]


class PipelineIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.config = PipelineConfig()
        self.budget = BudgetConfig()

    def _new_workdir(self) -> Path:
        work_dir = Path(tempfile.mkdtemp(prefix="mmpipe-it-"))
        self.addCleanup(shutil.rmtree, work_dir, ignore_errors=True)
        generate_manifest(work_dir, self.config, num_pairs=9)
        return work_dir

    def _run(self, work_dir: Path, **budget_overrides) -> dict:
        requested = {
            key: budget_overrides.pop(key)
            for key in ("requested_workers", "requested_batch")
            if key in budget_overrides
        }
        budget = BudgetConfig(**budget_overrides) if budget_overrides else self.budget
        pipeline = Pipeline(work_dir, self.config, budget, **requested)
        return pipeline.run(load_manifest(work_dir / "manifest.jsonl")).report

    def test_two_independent_runs_are_byte_identical(self):
        dir_a = self._new_workdir()
        dir_b = self._new_workdir()
        report_a = self._run(dir_a)
        report_b = self._run(dir_b)
        for stage in ("image", "text", "fusion", "score"):
            self.assertEqual(
                report_a["stages"][stage]["embeddings_checksum"],
                report_b["stages"][stage]["embeddings_checksum"],
            )
        self.assertEqual(
            (dir_a / "results.json").read_bytes(),
            (dir_b / "results.json").read_bytes(),
        )

    def test_resume_after_force_kill_matches_uninterrupted_run(self):
        clean_dir = self._new_workdir()
        clean_report = self._run(clean_dir)

        kill_dir = self._new_workdir()
        env = dict(os.environ)
        env["MMPIPE_KILL_STAGE"] = "fusion"
        env["MMPIPE_KILL_PAIR"] = "pair_0002"
        killed = subprocess.run(
            [sys.executable, "-m", "mmpipe.cli", "run", str(kill_dir)],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(killed.returncode, 0)
        # Image and text committed; fusion staging must be left uncommitted.
        self.assertTrue((kill_dir / "snapshots" / "image" / "v1" / "COMMITTED").exists())
        self.assertTrue((kill_dir / "snapshots" / "text" / "v1" / "COMMITTED").exists())
        fusion_dir = kill_dir / "snapshots" / "fusion"
        self.assertTrue(any(p.name.startswith(".tmp-") for p in fusion_dir.iterdir()))
        self.assertFalse(list(fusion_dir.glob("v*")))

        # Restart without the kill env: committed stages are reused, fusion rebuilt.
        resumed = Pipeline(kill_dir, self.config, self.budget).run(
            load_manifest(kill_dir / "manifest.jsonl")
        )
        for stage in ("image", "text"):
            self.assertEqual(resumed.report["stages"][stage]["status"], "reused")
            self.assertEqual(
                set(resumed.report["provenance"][stage]["reused_pair_ids"]),
                set(resumed.report["pair_ids_canonical"]),
            )
        self.assertEqual(resumed.report["stages"]["fusion"]["status"], "recomputed")

        for stage in ("image", "text", "fusion", "score"):
            self.assertEqual(
                resumed.report["stages"][stage]["embeddings_checksum"],
                clean_report["stages"][stage]["embeddings_checksum"],
            )
        self.assertEqual(
            (kill_dir / "results.json").read_bytes(),
            (clean_dir / "results.json").read_bytes(),
        )
        # No leftover staging directory after a clean commit.
        self.assertFalse(list(fusion_dir.glob(".tmp-*")))

    def test_snapshot_corruption_returns_business_code_and_force_rebuild_repairs(self):
        work_dir = self._new_workdir()
        self._run(work_dir)
        corrupt_snapshot(work_dir, "text", mode="tamper")
        with self.assertRaises(SnapshotCorruptError) as ctx:
            Pipeline(work_dir, self.config, self.budget).run(
                load_manifest(work_dir / "manifest.jsonl")
            )
        self.assertEqual(ctx.exception.code, "E_SNAPSHOT_CORRUPT")
        self.assertEqual(ctx.exception.stage, "text")
        self.assertEqual(ctx.exception.version, 1)

        repaired = Pipeline(work_dir, self.config, self.budget, force_rebuild=True).run(
            load_manifest(work_dir / "manifest.jsonl")
        )
        outcome = verify_snapshots(
            work_dir, load_manifest(work_dir / "manifest.jsonl"), self.config
        )
        self.assertTrue(outcome["ok"])
        self.assertEqual(repaired.report["stages"]["text"]["status"], "recomputed")

    def test_resource_limit_business_code_and_degradation_report(self):
        work_dir = self._new_workdir()
        with self.assertRaises(ResourceLimitError) as ctx:
            self._run(work_dir, max_stage_pairs=2, overflow_policy="reject")
        self.assertEqual(ctx.exception.code, "E_RESOURCE_LIMIT")

        degraded = self._run(
            work_dir, max_batch_pairs=2, max_workers=1, requested_workers=3, requested_batch=8
        )
        for stage in ("image", "text", "fusion", "score"):
            scheduling = degraded["stages"][stage]["scheduling"]
            self.assertTrue(scheduling["degraded"])
            self.assertTrue(scheduling["rationale"])
            self.assertLessEqual(scheduling["chosen_batch"], 2)
            self.assertEqual(scheduling["chosen_workers"], 1)

    def test_pair_failure_is_attributed_and_blocks_commit(self):
        work_dir = self._new_workdir()
        manifest_path = work_dir / "manifest.jsonl"
        lines = manifest_path.read_text().splitlines()
        record = json.loads(lines[0])
        record["text_tokens"] = []
        lines[0] = json.dumps(record, sort_keys=True)
        manifest_path.write_text("\n".join(lines) + "\n")

        pipeline = Pipeline(work_dir, self.config, self.budget)
        with self.assertRaises(PipelineExecutionError) as ctx:
            pipeline.run(load_manifest(manifest_path))
        failure = ctx.exception.failures[0]
        self.assertEqual(failure["code"], "E_TEXT_EMPTY")
        self.assertEqual(failure["pair_id"], record["pair_id"])
        self.assertEqual(failure["stage"], "text")
        # The text stage must not have a committed version (only a staging dir).
        self.assertFalse(list((work_dir / "snapshots" / "text").glob("v*")))
        self.assertTrue(list((work_dir / "snapshots" / "text").glob(".tmp-*")))

    def test_independent_verifier_detects_inconsistency(self):
        work_dir = self._new_workdir()
        self._run(work_dir)
        # Remove an embedding then rewrite the checksum over the reduced pair set,
        # so store-level verification passes but cross-stage consistency fails.
        score_manifest = work_dir / "snapshots" / "score" / "v1" / "manifest.json"
        manifest = json.loads(score_manifest.read_text())
        removed = manifest["pair_ids"].pop()
        embedding = work_dir / "snapshots" / "score" / "v1" / "embeddings" / f"{removed}.npy"
        embedding.unlink()
        from mmpipe.store import SnapshotStore

        embeddings_dir = work_dir / "snapshots" / "score" / "v1" / "embeddings"
        manifest["embeddings_checksum"] = SnapshotStore._compute_embeddings_checksum(
            embeddings_dir, manifest["pair_ids"]
        )
        score_manifest.write_text(json.dumps(manifest, sort_keys=True, indent=2))
        with self.assertRaises(InconsistentError) as ctx:
            verify_snapshots(work_dir, load_manifest(work_dir / "manifest.jsonl"), self.config)
        self.assertEqual(ctx.exception.code, "E_INCONSISTENT")
        self.assertTrue(ctx.exception.category)
        self.assertEqual(ctx.exception.version, 1)


if __name__ == "__main__":
    unittest.main()

