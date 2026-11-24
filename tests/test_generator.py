from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from generate_samples import generate


class GeneratorTests(unittest.TestCase):
    def test_fixed_seed_generation_is_byte_identical(self) -> None:
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            first_root = Path(first) / "sample_data"
            second_root = Path(second) / "sample_data"
            generate(first_root, seed=20261125)
            generate(second_root, seed=20261125)

            def hashes(root: Path) -> dict[str, str]:
                return {
                    path.relative_to(root).as_posix(): hashlib.sha256(
                        path.read_bytes()
                    ).hexdigest()
                    for path in root.rglob("*")
                    if path.is_file()
                }

            first_hashes = hashes(first_root)
            self.assertEqual(first_hashes, hashes(second_root))
            self.assertEqual(len(first_hashes), 9)
            self.assertTrue((first_root / "state").is_dir())
            self.assertEqual(list((first_root / "state").iterdir()), [])


if __name__ == "__main__":
    unittest.main()
