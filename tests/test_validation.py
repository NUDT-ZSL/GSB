from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from lbdiag.errors import (
    E_BAD_DIAGNOSTICS,
    E_BAD_INSTANCE,
    E_BAD_PLAN,
    E_SAMPLE_MISMATCH,
    BusinessError,
)
from lbdiag.model import parse_sample

from .helpers import ROOT, clone, sample_raw, write_sample


class ValidationTests(unittest.TestCase):
    def assert_code(self, code: str, raw: dict) -> None:
        with self.assertRaises(BusinessError) as caught:
            parse_sample(raw)
        self.assertEqual(caught.exception.code, code)

    def test_valid_sample(self) -> None:
        config = parse_sample(sample_raw())
        self.assertEqual(len(config.instances), 7)
        self.assertEqual(config.plan.window_length, 8)

    def test_duplicate_instance(self) -> None:
        raw = clone(sample_raw())
        raw["diagnostics"].pop("sample_sha256", None)
        raw["instances"][1]["id"] = raw["instances"][0]["id"]
        self.assert_code(E_BAD_INSTANCE, raw)

    def test_non_loopback_address(self) -> None:
        raw = clone(sample_raw())
        raw["diagnostics"].pop("sample_sha256", None)
        raw["instances"][0]["address"] = "http://192.0.2.10:80/health"
        self.assert_code(E_BAD_INSTANCE, raw)

    def test_bad_weight(self) -> None:
        raw = clone(sample_raw())
        raw["diagnostics"].pop("sample_sha256", None)
        raw["instances"][0]["weight"] = 0
        self.assert_code(E_BAD_INSTANCE, raw)

    def test_interval_must_exceed_timeout(self) -> None:
        raw = clone(sample_raw())
        raw["diagnostics"].pop("sample_sha256", None)
        raw["plan"]["timeout_ms"] = raw["plan"]["interval_ms"]
        self.assert_code(E_BAD_PLAN, raw)

    def test_jitter_range(self) -> None:
        raw = clone(sample_raw())
        raw["diagnostics"].pop("sample_sha256", None)
        raw["plan"]["jitter_ratio"] = 1
        self.assert_code(E_BAD_PLAN, raw)

    def test_fixed_scale(self) -> None:
        raw = clone(sample_raw())
        raw["diagnostics"].pop("sample_sha256", None)
        raw["diagnostics"]["fixed_scale"] = 0
        self.assert_code(E_BAD_DIAGNOSTICS, raw)

    def test_digest_mismatch(self) -> None:
        raw = clone(sample_raw())
        raw["diagnostics"]["sample_sha256"] = "0" * 64
        self.assert_code(E_SAMPLE_MISMATCH, raw)

    def test_invalid_cli_input_writes_no_database(self) -> None:
        raw = clone(sample_raw())
        raw["diagnostics"].pop("sample_sha256", None)
        raw["instances"][0]["weight"] = 0
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            sample_path = write_sample(directory / "bad.json", raw)
            db_path = directory / "must-not-exist.db"
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "lbdiag",
                    "run",
                    "--sample",
                    str(sample_path),
                    "--db",
                    str(db_path),
                    "--log-dir",
                    str(directory / "logs"),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 2, completed.stderr)
            output = json.loads(completed.stdout)
            self.assertEqual(output["error"]["code"], E_BAD_INSTANCE)
            self.assertFalse(db_path.exists())
            self.assertFalse((directory / "logs").exists())

    def test_unknown_cli_instance_writes_no_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            db_path = directory / "must-not-exist.db"
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "lbdiag",
                    "run",
                    "--sample",
                    str(ROOT / "samples" / "diagnostic_sample.json"),
                    "--db",
                    str(db_path),
                    "--log-dir",
                    str(directory / "logs"),
                    "--instance",
                    "not-present",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 2, completed.stderr)
            output = json.loads(completed.stdout)
            self.assertEqual(output["error"]["code"], E_BAD_INSTANCE)
            self.assertFalse(db_path.exists())


if __name__ == "__main__":
    unittest.main()

