from __future__ import annotations

import tempfile
import unittest

from sceneopt.config import PipelineConfig, ResourceLimits
from sceneopt.errors import E_RESOURCE_LIMIT, ResourceLimitExceeded
from sceneopt.pipeline import Pipeline
from sceneopt.samples import ensure_samples


class ResourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        ensure_samples("samples")

    def test_face_limit_errors_without_degradation(self) -> None:
        config = PipelineConfig(
            limits=ResourceLimits(
                max_faces_per_scene=20,
                allow_degrade=False,
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ResourceLimitExceeded) as context:
                Pipeline(
                    "samples/scenes.jsonl",
                    cache_dir=directory,
                    config=config,
                ).run()
            self.assertEqual(context.exception.code, E_RESOURCE_LIMIT)
            self.assertEqual(
                context.exception.details["resource"], "faces_per_scene"
            )

    def test_limits_degrade_and_mark(self) -> None:
        config = PipelineConfig(
            parallel_workers=8,
            limits=ResourceLimits(
                max_faces_per_scene=20,
                max_parallel_recompute=2,
                allow_degrade=True,
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            result = Pipeline(
                "samples/scenes.jsonl", cache_dir=directory, config=config
            ).run()
            self.assertEqual(result.report["resource"]["effective_parallelism"], 2)
            self.assertTrue(result.report["resource"]["degraded"])
            for run in result.scene_runs.values():
                self.assertEqual(run.effective_face_budget, 20)
                stats = run.artifacts["geometry_simplify"].payload["stats"]
                self.assertLessEqual(stats["faces_after"], 20)
                self.assertTrue(run.resource_degraded)

    def test_resident_memory_limit(self) -> None:
        config = PipelineConfig(
            limits=ResourceLimits(max_resident_bytes=1, allow_degrade=True)
        )
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ResourceLimitExceeded) as context:
                Pipeline(
                    "samples/scenes.jsonl",
                    cache_dir=directory,
                    config=config,
                ).run()
            self.assertEqual(
                context.exception.details["resource"],
                "resident_mesh_texture_bytes",
            )


if __name__ == "__main__":
    unittest.main()

