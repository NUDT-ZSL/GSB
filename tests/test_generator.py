from __future__ import annotations

import hashlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from .helpers import ROOT


class GeneratorTests(unittest.TestCase):
    def test_fixed_seed_is_byte_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = root / "first"
            second = root / "second"
            for output in (first, second):
                result = subprocess.run(
                    [
                        sys.executable,
                        str(ROOT / "scripts" / "generate_sample.py"),
                        "--output-dir",
                        str(output),
                        "--seed",
                        "20261125",
                    ],
                    cwd=ROOT,
                    text=True,
                    capture_output=True,
                    timeout=20,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
            names = sorted(path.name for path in first.iterdir())
            self.assertEqual(names, sorted(path.name for path in second.iterdir()))
            for name in names:
                self.assertEqual(
                    hashlib.sha256((first / name).read_bytes()).hexdigest(),
                    hashlib.sha256((second / name).read_bytes()).hexdigest(),
                    name,
                )


if __name__ == "__main__":
    unittest.main()

