from __future__ import annotations

import copy
import json
import unittest

from lockledger.errors import LedgerError
from lockledger.samples import main_scenario, write_samples
from lockledger.validation import validate_scenario

from .support import ROOT, fresh_dir, run_cli


class ValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.raw = copy.deepcopy(main_scenario())

    def assert_code(self, expected: str) -> None:
        with self.assertRaises(LedgerError) as caught:
            validate_scenario(self.raw, source_path="<test>")
        self.assertEqual(caught.exception.code, expected)

    def test_bad_scale(self) -> None:
        self.raw["config"]["scale"] = 0
        self.assert_code("E_BAD_SCALE")

    def test_negative_tolerance(self) -> None:
        self.raw["config"]["tolerance"] = "-1/2"
        self.assert_code("E_BAD_CONFIG")

    def test_duplicate_table(self) -> None:
        self.raw["database"]["tables"].append(
            copy.deepcopy(self.raw["database"]["tables"][0])
        )
        self.assert_code("E_DUPLICATE_TABLE")

    def test_duplicate_column(self) -> None:
        self.raw["database"]["tables"][0]["columns"].append(
            copy.deepcopy(self.raw["database"]["tables"][0]["columns"][0])
        )
        self.assert_code("E_DUPLICATE_COLUMN")

    def test_unknown_table(self) -> None:
        self.raw["transactions"][0]["operations"][0]["table"] = "missing_table"
        self.assert_code("E_NO_TABLE")

    def test_unknown_column(self) -> None:
        self.raw["transactions"][0]["operations"][0]["values"] = {"missing": 1}
        self.assert_code("E_NO_COLUMN")

    def test_bad_operation_enum(self) -> None:
        self.raw["transactions"][0]["operations"][0]["type"] = "delete"
        self.assert_code("E_BAD_OPERATION")

    def test_bad_isolation_enum(self) -> None:
        self.raw["transactions"][0]["isolation"] = "CHAOS"
        self.assert_code("E_BAD_ISOLATION")

    def test_bad_budget(self) -> None:
        self.raw["transactions"][0]["wait_budget_ms"] = 0
        self.assert_code("E_BAD_BUDGET")

    def test_bad_retry(self) -> None:
        self.raw["transactions"][0]["max_retries"] = 0
        self.assert_code("E_BAD_RETRY")

    def test_missing_section(self) -> None:
        del self.raw["config"]
        self.assert_code("E_BAD_SCRIPT")

    def test_invalid_cli_creates_no_run_artifacts(self) -> None:
        self.raw["config"]["scale"] = 0
        sample = fresh_dir("validation") / "bad.json"
        sample.write_text(json.dumps(self.raw), encoding="utf-8")
        workdir = ROOT / "tests" / ".tmp" / "validation" / "run"
        completed = run_cli(
            "run",
            "--sample",
            str(sample),
            "--workdir",
            str(workdir),
        )
        output = json.loads(completed.stdout)
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(output["error"]["code"], "E_BAD_SCALE")
        self.assertFalse(workdir.exists())

    def test_sample_generator_is_deterministic(self) -> None:
        first = fresh_dir("generator-a")
        second = fresh_dir("generator-b")
        result_a = write_samples(first)
        result_b = write_samples(second)
        self.assertEqual(
            result_a["database"]["content_sha256"],
            result_b["database"]["content_sha256"],
        )
        for name in ("scenario.json", "notstarted.json", "manifest.json"):
            self.assertEqual(
                (first / name).read_bytes(),
                (second / name).read_bytes(),
            )


if __name__ == "__main__":
    unittest.main()

