from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from gsb.errors import GSBError
from gsb.util import sha256_json
from gsb.validation import load_sample

from tests.helpers import copy_sample, load_json, run_cli, write_json


class ValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def sample(self) -> Path:
        return copy_sample(self.root / "sample")

    def assert_code(self, path: Path, code: str) -> GSBError:
        with self.assertRaises(GSBError) as caught:
            load_sample(path)
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_bad_column_length(self) -> None:
        sample = self.sample()
        path = sample / "batches" / "batch-0001.json"
        raw = load_json(path)
        raw["columns"]["region"].pop()
        write_json(path, raw)
        error = self.assert_code(sample, "E_BAD_BATCH")
        self.assertIn("column length", error.message)

    def test_bad_digest(self) -> None:
        sample = self.sample()
        path = sample / "batches" / "batch-0001.json"
        raw = load_json(path)
        raw["digest"] = "0" * 64
        write_json(path, raw)
        error = self.assert_code(sample, "E_BAD_BATCH")
        self.assertIn("digest", error.message)

    def test_batch_sequence_strictly_increasing(self) -> None:
        sample = self.sample()
        path = sample / "batches" / "batch-0002.json"
        raw = load_json(path)
        raw["batch_index"] = 1
        content = {
            "batch_index": raw["batch_index"],
            "row_count": raw["row_count"],
            "columns": raw["columns"],
        }
        raw["digest"] = sha256_json(content)
        write_json(path, raw)
        self.assert_code(sample, "E_BAD_BATCH")

    def test_duplicate_json_column_key(self) -> None:
        sample = self.sample()
        path = sample / "batches" / "batch-0001.json"
        text = (
            '{"batch_index":1,"row_count":2000,"digest":"'
            + "0" * 64
            + '","columns":{"region":[],"region":[]}}'
        )
        path.write_text(text, encoding="utf-8")
        error = self.assert_code(sample, "E_BAD_BATCH")
        self.assertIn("invalid", error.message)

    def test_bad_query_column(self) -> None:
        sample = self.sample()
        path = sample / "queries.json"
        raw = load_json(path)
        raw["queries"][0]["aggregates"][0]["column"] = "missing-column"
        write_json(path, raw)
        self.assert_code(sample, "E_BAD_QUERY")

    def test_bad_window(self) -> None:
        sample = self.sample()
        path = sample / "queries.json"
        raw = load_json(path)
        raw["queries"][1]["window"] = {"length": 3, "step": 4}
        write_json(path, raw)
        self.assert_code(sample, "E_BAD_QUERY")

    def test_bad_config_scale(self) -> None:
        sample = self.sample()
        path = sample / "config.json"
        raw = load_json(path)
        raw["fixed_scale"] = 0
        write_json(path, raw)
        self.assert_code(sample, "E_BAD_CONFIG")

    def test_invalid_sample_writes_no_state(self) -> None:
        sample = self.sample()
        path = sample / "config.json"
        raw = load_json(path)
        raw["float_tolerance"] = -1
        write_json(path, raw)
        db = self.root / "state" / "should-not-exist.sqlite"
        result = run_cli(
            [
                "run",
                "--sample",
                str(sample),
                "--db",
                str(db),
                "--work",
                str(self.root / "work"),
                "--out",
                str(self.root / "out"),
            ]
        )
        self.assertEqual(result.returncode, 3)
        self.assertIn("E_BAD_CONFIG", result.stderr)
        self.assertFalse(db.exists())
        self.assertFalse((self.root / "work").exists())


if __name__ == "__main__":
    unittest.main()

