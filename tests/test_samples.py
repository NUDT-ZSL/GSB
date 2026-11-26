from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from dialog_state_engine.samples import generate_samples


class SampleDeterminismTests(unittest.TestCase):
    def test_sample_generation_is_byte_deterministic(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dialog-samples-") as temp:
            root = Path(temp)
            first = root / "first"
            second = root / "second"
            generate_samples(first)
            generate_samples(second)
            first_files = {
                path.relative_to(first): path.read_bytes()
                for path in first.rglob("*")
                if path.is_file()
            }
            second_files = {
                path.relative_to(second): path.read_bytes()
                for path in second.rglob("*")
                if path.is_file()
            }
            self.assertEqual(first_files, second_files)


if __name__ == "__main__":
    unittest.main()

