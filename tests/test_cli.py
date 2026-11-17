from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / "run.py"
TABLES = ROOT / "data" / "sample_tables.json"
QUERY = ROOT / "data" / "sample_query.json"


class CliInjectionTests(unittest.TestCase):
    def test_three_injections_write_business_codes_and_event_logs(self) -> None:
        expected = {"nullmap": "E_NULLMAP", "scalef": "E_SCALEF", "part": "E_PART"}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for injection, code in expected.items():
                output = root / injection
                completed = subprocess.run(
                    [
                        sys.executable,
                        str(RUN),
                        "run",
                        "--tables",
                        str(TABLES),
                        "--query",
                        str(QUERY),
                        "--inject",
                        injection,
                        "--output",
                        str(output),
                        "--quiet",
                    ],
                    cwd=ROOT,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(completed.returncode, 2, completed.stderr)
                error = json.loads((output / "error.json").read_text(encoding="utf-8"))
                self.assertEqual(error["code"], code)
                events = (output / "events.jsonl").read_text(encoding="utf-8").splitlines()
                self.assertGreaterEqual(len(events), 2)
                self.assertEqual(json.loads(events[-1])["code"], code)

    def test_direct_invalid_parameters_use_business_codes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            completed = subprocess.run(
                [
                    sys.executable,
                    str(RUN),
                    "run",
                    "--tables",
                    str(TABLES),
                    "--query",
                    str(QUERY),
                    "--scale-factor",
                    "25",
                    "--output",
                    str(root / "bad_scale"),
                    "--quiet",
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 2)
            self.assertEqual(json.loads((root / "bad_scale" / "error.json").read_text(encoding="utf-8"))["code"], "E_SCALEF")


if __name__ == "__main__":
    unittest.main()
