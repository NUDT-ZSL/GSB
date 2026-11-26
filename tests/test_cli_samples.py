from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from index_diag.samples import generate_samples


class CliSampleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_fixed_seed_is_byte_deterministic(self) -> None:
        first = generate_samples(self.root / "first", seed=99)
        second = generate_samples(self.root / "second", seed=99)
        self.assertEqual(first["batch_hashes"], second["batch_hashes"])
        self.assertEqual(first["replacement_hash"], second["replacement_hash"])
        for relative in first["batches"]:
            self.assertEqual(
                (self.root / "first" / relative).read_bytes(),
                (self.root / "second" / relative).read_bytes(),
            )

    def test_cli_build_query_last_code_and_shell(self) -> None:
        manifest = generate_samples(self.root / "samples", seed=5)
        samples = self.root / "samples"
        config = samples / manifest["config_v2"]
        work = self.root / "work"
        command = [
            sys.executable,
            "-m",
            "index_diag",
            "run",
            "--work",
            str(work),
            "--config",
            str(config),
        ]
        for relative in manifest["batches"]:
            command.extend(["--batch", str(samples / relative)])
        completed = subprocess.run(
            command,
            cwd=Path(__file__).resolve().parents[1],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(json.loads(completed.stdout)["ok"])

        query = subprocess.run(
            [
                sys.executable,
                "-m",
                "index_diag",
                "query",
                "--work",
                str(work),
                "--term",
                "search",
            ],
            cwd=Path(__file__).resolve().parents[1],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(query.returncode, 0, query.stderr)
        self.assertTrue(json.loads(query.stdout)["ok"])

        last_code = subprocess.run(
            [
                sys.executable,
                "-m",
                "index_diag",
                "last-code",
                "--work",
                str(work),
            ],
            cwd=Path(__file__).resolve().parents[1],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(last_code.returncode, 0, last_code.stderr)
        self.assertEqual(json.loads(last_code.stdout)["last_code"]["code"], "OK")

        shell = subprocess.run(
            [
                sys.executable,
                "-m",
                "index_diag",
                "shell",
                "--work",
                str(work),
            ],
            cwd=Path(__file__).resolve().parents[1],
            input="postings search\nlast-code\nquit\n",
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(shell.returncode, 0, shell.stderr)
        self.assertIn("index-diag shell", shell.stdout)
        self.assertIn('"term": "search"', shell.stdout)


if __name__ == "__main__":
    unittest.main()
