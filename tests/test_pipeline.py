from __future__ import annotations

import tempfile
import unittest

from sceneopt.errors import E_INCREMENTAL_INCONSISTENT, IncrementalInconsistent
from sceneopt.models import load_scene_manifest
from sceneopt.pipeline import Pipeline, STAGE_NAMES
from sceneopt.samples import ensure_samples
from sceneopt.verify import verify_snapshot


class PipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        ensure_samples("samples")

    def test_baseline_revision_reuse_and_full_replay(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            baseline = Pipeline(
                "samples/scenes.jsonl", cache_dir=directory
            ).run()
            revision = Pipeline(
                "samples/revision.jsonl", cache_dir=directory
            ).run()

            self.assertEqual(baseline.snapshot.version, 1)
            self.assertEqual(revision.snapshot.version, 2)
            self.assertTrue(revision.report["snapshot_replaced"])
            self.assertTrue(revision.report["snapshot_checksum_changed"])
            self.assertTrue(revision.report["full_replay_verified"])
            self.assertTrue(revision.report["self_check"]["passed"])
            self.assertEqual(
                revision.report["affected_scenes"],
                ["scene-alpha", "scene-beta", "scene-gamma"],
            )
            self.assertEqual(revision.report["reused_scenes"], ["scene-delta"])
            self.assertEqual(
                revision.report["stage_reused"]["ingest_register"],
                ["scene-beta", "scene-delta", "scene-gamma"],
            )
            for stage in STAGE_NAMES:
                recomputed = set(revision.report["stage_recomputed"][stage])
                reused = set(revision.report["stage_reused"][stage])
                self.assertFalse(recomputed & reused)
                self.assertEqual(
                    recomputed | reused, set(revision.report["scene_ids"])
                )

            for scene_run in revision.scene_runs.values():
                compare = scene_run.artifacts["block_compare"].payload
                self.assertTrue(compare["comparison"]["exact_equal"])
                self.assertEqual(compare["comparison"]["max_abs_diff"], 0.0)
                intervals = [
                    level["error_interval"]
                    for level in compare["error_spectrum"]["levels"]
                ]
                self.assertTrue(
                    all(interval[0] <= interval[1] for interval in intervals)
                )

            verification = verify_snapshot(
                load_scene_manifest("samples/revision.jsonl"),
                revision.snapshot,
                revision.cache,
            )
            self.assertTrue(verification["passed"])
            self.assertEqual(
                verification["registration_max_residual_delta"], 0.0
            )

    def test_incremental_matches_fresh_full_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as incremental_dir:
            Pipeline("samples/scenes.jsonl", cache_dir=incremental_dir).run()
            incremental = Pipeline(
                "samples/revision.jsonl", cache_dir=incremental_dir
            ).run()
            with tempfile.TemporaryDirectory() as full_dir:
                full = Pipeline(
                    "samples/revision.jsonl", cache_dir=full_dir
                ).run()
                self.assertEqual(
                    set(incremental.scene_runs), set(full.scene_runs)
                )
                for scene_id in sorted(full.scene_runs):
                    for stage in STAGE_NAMES:
                        self.assertEqual(
                            incremental.scene_runs[scene_id]
                            .artifacts[stage]
                            .checksum,
                            full.scene_runs[scene_id].artifacts[stage].checksum,
                            f"{scene_id}/{stage}",
                        )

    def test_baseline_is_byte_deterministic_across_clean_caches(self) -> None:
        with tempfile.TemporaryDirectory() as first_dir:
            first = Pipeline("samples/scenes.jsonl", cache_dir=first_dir).run()
            with tempfile.TemporaryDirectory() as second_dir:
                second = Pipeline(
                    "samples/scenes.jsonl", cache_dir=second_dir
                ).run()
                for scene_id in sorted(first.scene_runs):
                    for stage in STAGE_NAMES:
                        self.assertEqual(
                            first.scene_runs[scene_id].artifacts[stage].checksum,
                            second.scene_runs[scene_id].artifacts[stage].checksum,
                        )
                self.assertEqual(first.snapshot.checksum, second.snapshot.checksum)

    def test_incremental_inconsistency_names_first_scene(self) -> None:
        class CorruptingPipeline(Pipeline):
            def _fresh_scene_payloads(self, scene, effective_face_budget, cache):
                payloads = super()._fresh_scene_payloads(
                    scene, effective_face_budget, cache
                )
                payloads["ingest_register"] = dict(payloads["ingest_register"])
                payloads["ingest_register"]["tampered"] = True
                return payloads

        with tempfile.TemporaryDirectory() as directory:
            Pipeline("samples/scenes.jsonl", cache_dir=directory).run()
            with self.assertRaises(IncrementalInconsistent) as context:
                CorruptingPipeline(
                    "samples/revision.jsonl", cache_dir=directory
                ).run()
            self.assertEqual(context.exception.code, E_INCREMENTAL_INCONSISTENT)
            self.assertIn(
                context.exception.details["first_deviating_scene_id"],
                {"scene-alpha", "scene-beta", "scene-delta", "scene-gamma"},
            )


if __name__ == "__main__":
    unittest.main()

