from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from signhand.pipeline import run_pipeline
from signhand.store import VersionedArtifactStore, build_artifact_manifest


class PipelineTests(unittest.TestCase):
    def _small_project(self, directory: Path) -> tuple[Path, Path]:
        source = json.loads(Path("examples/generated/sign_actions.json").read_text())
        source["project_version"] = "test-v1"
        source["frame_range"] = [0, 2]
        source["render"]["width"] = 40
        source["render"]["height"] = 40
        source["render"]["max_steps"] = 256
        project_path = directory / "project.json"
        project_path.write_text(json.dumps(source), encoding="utf-8")
        moments_path = directory / "key_moments.json"
        moments_path.write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "key_moments": [
                        {"frame": 0, "label": "neutral"},
                        {"frame": 2, "label": "curl"},
                    ],
                }
            ),
            encoding="utf-8",
        )
        return project_path, moments_path

    def test_pipeline_versioning_and_checksums(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project_path, moments_path = self._small_project(root)
            output_a = root / "out_a"
            result = run_pipeline(
                project_path,
                moments_path,
                output_a,
                repeat_check=True,
            )
            self.assertEqual(result.report["run_status"], "COMPLETE")
            self.assertTrue(result.report["repeatability_passed"])
            self.assertTrue((result.version_dir / "render/frame_0000.png").is_file())
            self.assertTrue((result.version_dir / "render/frame_0002.png").is_file())
            self.assertFalse((result.version_dir / "render/frame_0003.png").exists())
            store = VersionedArtifactStore(output_a)
            self.assertEqual(store.resolve("test-v1"), result.version_dir)
            manifest = store.load_manifest()
            for entry in manifest["files"]:
                self.assertTrue((result.version_dir / entry["path"]).is_file())

            output_b = root / "out_b"
            result_b = run_pipeline(
                project_path,
                moments_path,
                output_b,
                repeat_check=True,
            )
            manifest_a = build_artifact_manifest(result.version_dir)
            manifest_b = build_artifact_manifest(result_b.version_dir)
            self.assertEqual(manifest_a, manifest_b)


if __name__ == "__main__":
    unittest.main()

