from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from patrol_stats.generator import generate_dataset


class DeterminismTests(unittest.TestCase):
    def test_same_seed_produces_byte_identical_exports(self) -> None:
        with tempfile.TemporaryDirectory() as left_dir, tempfile.TemporaryDirectory() as right_dir:
            left = Path(left_dir)
            right = Path(right_dir)
            manifest_left = generate_dataset(
                left / "left.db",
                left / "generated",
                seed=12345,
                log_count=320,
                query_count=8,
            )
            manifest_right = generate_dataset(
                right / "right.db",
                right / "generated",
                seed=12345,
                log_count=320,
                query_count=8,
            )
            self.assertEqual(manifest_left, manifest_right)
            left_files = sorted((left / "generated").glob("*"))
            right_files = sorted((right / "generated").glob("*"))
            self.assertEqual(
                [path.name for path in left_files],
                [path.name for path in right_files],
            )
            for left_path, right_path in zip(left_files, right_files):
                self.assertEqual(left_path.read_bytes(), right_path.read_bytes())

    def test_different_seed_changes_data(self) -> None:
        with tempfile.TemporaryDirectory() as left_dir, tempfile.TemporaryDirectory() as right_dir:
            left = Path(left_dir)
            right = Path(right_dir)
            first = generate_dataset(
                left / "left.db",
                left / "generated",
                seed=1,
                log_count=320,
                query_count=8,
            )
            second = generate_dataset(
                right / "right.db",
                right / "generated",
                seed=2,
                log_count=320,
                query_count=8,
            )
            self.assertNotEqual(first["data_sha256"], second["data_sha256"])


if __name__ == "__main__":
    unittest.main()
