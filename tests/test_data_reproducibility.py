from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from query_understanding.experiments import verify_data_manifest
from query_understanding.util import file_sha256, read_json

from tests.common import DATA

import importlib.util


def _load_generator():
    path = DATA.parent / "scripts" / "generate_data.py"
    spec = importlib.util.spec_from_file_location("generate_data", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class DataReproducibilityTest(unittest.TestCase):
    def test_committed_data_manifest_is_intact(self):
        report = verify_data_manifest(DATA)
        self.assertTrue(report["ok"])
        self.assertEqual(report["small_query_count"], 96)
        self.assertEqual(report["large_query_count"], 576)

    def test_regeneration_is_byte_identical(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "data"
            generator = _load_generator()
            generated = generator.generate_data(output)
            manifest = read_json(DATA / "manifest.json")
            self.assertEqual(
                generated["manifest_checksum"], manifest["manifest_checksum"]
            )
            for name in manifest["files"]:
                self.assertEqual(
                    file_sha256(output / name),
                    manifest["files"][name]["sha256"],
                    name,
                )


if __name__ == "__main__":
    unittest.main()

