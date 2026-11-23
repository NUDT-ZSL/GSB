from __future__ import annotations

import os
import tempfile
import unittest

from sceneopt.models import load_scene_manifest
from sceneopt.pipeline import Pipeline
from sceneopt.samples import ensure_samples
from sceneopt.verify import verify_snapshot


class VerificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        ensure_samples("samples")

    def test_verifier_reports_corrupt_referenced_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = Pipeline(
                "samples/scenes.jsonl", cache_dir=directory
            ).run()
            reference = result.snapshot.scenes["scene-alpha"]["stages"][
                "ingest_register"
            ]
            path = result.cache._path(
                "ingest_register", reference["cache_key"]
            )
            with open(path, "wb") as handle:
                handle.write(b"broken")
            report = verify_snapshot(
                load_scene_manifest("samples/scenes.jsonl"),
                result.snapshot,
                result.cache,
                raise_on_error=False,
            )
            self.assertFalse(report["passed"])
            self.assertTrue(
                any("unreadable cache artifact" in issue for issue in report["issues"])
            )


if __name__ == "__main__":
    unittest.main()

