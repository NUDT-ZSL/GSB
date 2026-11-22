from __future__ import annotations

import subprocess

from sft_pipeline.demo import run_recovery_demo
from sft_pipeline.engine import PipelineEngine
from sft_pipeline.storage import committed_stages
from sft_pipeline.workspace import Workspace

from .helpers import PROJECT_ROOT, WorkspaceTestCase


class RecoveryTests(WorkspaceTestCase):
    def test_forced_kill_after_commit_matches_full_run(self) -> None:
        workspace = self.make_workspace()
        report = run_recovery_demo(
            workspace, kill_stage="quality", mode="kill"
        )
        self.assertTrue(report["ok"], report)
        self.assertTrue(report["byte_identical"])
        self.assertTrue(report["version_sequences_identical"])
        self.assertNotEqual(report["forced_termination"]["returncode"], 0)
        self.assertEqual(
            report["recovery"]["run"]["started_with_stages"],
            ["ingest", "normalize", "quality"],
        )
        self.assertEqual(
            report["recovery"]["run"]["recomputed_stages"],
            ["dedupe", "sample", "export"],
        )

    def test_graceful_signal_stops_at_commit_boundary(self) -> None:
        workspace = self.make_workspace()
        report = run_recovery_demo(
            workspace, kill_stage="dedupe", mode="term"
        )
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["forced_termination"]["returncode"], 0)
        self.assertTrue(report["byte_identical"])
        recovery_workspace = workspace.root / "_demo" / "recovery"
        graceful = self.read_json(
            recovery_workspace / "state" / "graceful_stop.json"
        )
        self.assertTrue(graceful["stopped_gracefully"])
        self.assertEqual(graceful["at_stage"], "dedupe")
        state = PipelineEngine(Workspace(recovery_workspace)).store.load_manifest()
        self.assertEqual(
            committed_stages(state)[:4],
            ["ingest", "normalize", "quality", "dedupe"],
        )

    def test_mid_stage_forced_kill_discards_partial_work(self) -> None:
        full = self.make_workspace("full")
        interrupted = self.make_workspace("interrupted")
        full_run = self.run_pipeline(full)
        expected = self.export_bytes(full, full_run)

        completed = subprocess.run(
            self.worker_command(
                interrupted,
                "--fault-stage",
                "quality",
                "--fault-after-items",
                "5",
            ),
            cwd=str(PROJECT_ROOT),
            env=self.worker_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=45,
        )
        self.assertEqual(completed.returncode, 137, completed.stderr)
        engine = PipelineEngine(interrupted)
        state = engine.store.load_manifest()
        self.assertEqual(
            committed_stages(state), ["ingest", "normalize"]
        )
        self.assertFalse(
            (interrupted.snapshots_dir / "quality").exists()
            and any((interrupted.snapshots_dir / "quality").iterdir())
        )
        published_keys = list(interrupted.cache_keys_dir.glob("*.json"))
        self.assertEqual(len(published_keys), 2)

        resume = engine.run()
        self.assertEqual(resume.mode, "resume")
        self.assertEqual(
            resume.reused_snapshot_stages, ["ingest", "normalize"]
        )
        self.assertEqual(
            resume.recomputed_stages,
            ["quality", "dedupe", "sample", "export"],
        )
        self.assertEqual(
            self.export_bytes(interrupted, resume.to_dict()), expected
        )
        self.assertTrue(engine.self_check()["ok"])
