from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REQUIRED_FIELDS = {
    "job_id",
    "op_kind",
    "input_shape",
    "block_size",
    "axis",
    "eps",
    "dtype",
    "seed",
    "norm_mode",
}


class SampleGeneratorTests(unittest.TestCase):
    def test_fixed_seed_generation_is_byte_identical(self) -> None:
        with tempfile.TemporaryDirectory() as left, tempfile.TemporaryDirectory() as right:
            for directory in (left, right):
                completed = subprocess.run(
                    [
                        sys.executable,
                        str(ROOT / "scripts" / "generate_samples.py"),
                        "--output-dir",
                        directory,
                    ],
                    cwd=ROOT,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
            left_files = sorted(
                path.relative_to(left)
                for path in Path(left).rglob("*")
                if path.is_file()
            )
            right_files = sorted(
                path.relative_to(right)
                for path in Path(right).rglob("*")
                if path.is_file()
            )
            self.assertEqual(left_files, right_files)
            for relative in left_files:
                self.assertEqual(
                    (Path(left) / relative).read_bytes(),
                    (Path(right) / relative).read_bytes(),
                    str(relative),
                )

    def test_task_jsonl_has_required_fields_and_fixed_seeds(self) -> None:
        path = ROOT / "samples" / "tasks.jsonl"
        lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line]
        self.assertGreater(len(lines), 0)
        for line in lines:
            task = json.loads(line)
            self.assertEqual(set(task), REQUIRED_FIELDS)
            self.assertIn(task["op_kind"], {"softmax", "layernorm"})
            self.assertIn(task["dtype"], {"float32", "float64"})
            self.assertIsInstance(task["seed"], int)


if __name__ == "__main__":
    unittest.main()
