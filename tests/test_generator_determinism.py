from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from build_routing.generator import generate_artifacts


class GeneratorDeterminismTests(unittest.TestCase):
    def test_same_seed_produces_identical_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            a = generate_artifacts(first, seed=998877)
            b = generate_artifacts(second, seed=998877)
            self.assertEqual(a["manifest"]["sha256"], b["manifest"]["sha256"])
            self.assertEqual(
                json.loads((Path(first) / "events.json").read_text(encoding="utf-8")),
                json.loads((Path(second) / "events.json").read_text(encoding="utf-8")),
            )

    def test_different_seed_changes_generated_as_paths(self) -> None:
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            a = generate_artifacts(first, seed=1)
            b = generate_artifacts(second, seed=2)
            self.assertNotEqual(
                a["manifest"]["sha256"]["project_v1"],
                b["manifest"]["sha256"]["project_v1"],
            )


if __name__ == "__main__":
    unittest.main()
