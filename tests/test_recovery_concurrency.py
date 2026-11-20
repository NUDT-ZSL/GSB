from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.helpers import PYTHON, ROOT, run_cli


class RecoveryConcurrencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.sample = ROOT / "samples" / "tilted"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def paths(self, tag: str) -> tuple[Path, Path, Path]:
        return (
            self.root / f"{tag}.sqlite",
            self.root / f"{tag}-work",
            self.root / f"{tag}-out",
        )

    def run_fresh(self, tag: str) -> Path:
        db, work, out = self.paths(tag)
        result = run_cli(
            [
                "run",
                "--sample",
                str(self.sample),
                "--db",
                str(db),
                "--work",
                str(work),
                "--out",
                str(out),
                "--query",
                "tilted-main",
            ]
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return out / "tilted-main.result.json"

    def sha(self, path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def test_force_termination_during_stage_and_resume(self) -> None:
        baseline = self.run_fresh("baseline")
        db, work, out = self.paths("crash")
        crash = run_cli(
            [
                "run",
                "--sample",
                str(self.sample),
                "--db",
                str(db),
                "--work",
                str(work),
                "--out",
                str(out),
                "--query",
                "tilted-main",
                "--crash-at",
                "stage:3",
            ]
        )
        self.assertEqual(crash.returncode, 86)
        status = run_cli(["status", "--db", str(db), "--work", str(work)])
        self.assertEqual(status.returncode, 0)
        before = json.loads(status.stdout)["queries"][0]
        self.assertEqual(before["watermark"]["ordinal"], 0)
        resume = run_cli(
            [
                "resume",
                "--sample",
                str(self.sample),
                "--db",
                str(db),
                "--work",
                str(work),
                "--out",
                str(out),
                "--query",
                "tilted-main",
            ]
        )
        self.assertEqual(resume.returncode, 0, resume.stderr)
        report = json.loads(resume.stdout)["queries"][0]
        self.assertEqual(report["status"], "complete")
        self.assertEqual(report["recovery"]["prepared_recovered"], 3)
        self.assertEqual(self.sha(baseline), self.sha(out / "tilted-main.result.json"))
        verify = run_cli(
            [
                "verify",
                "--sample",
                str(self.sample),
                "--db",
                str(db),
                "--work",
                str(work),
                "--out",
                str(out),
                "--query",
                "tilted-main",
            ]
        )
        self.assertEqual(verify.returncode, 0, verify.stdout + verify.stderr)

    def test_force_termination_in_spill_transaction_and_resume(self) -> None:
        baseline = self.run_fresh("baseline2")
        db, work, out = self.paths("crash2")
        crash = run_cli(
            [
                "run",
                "--sample",
                str(self.sample),
                "--db",
                str(db),
                "--work",
                str(work),
                "--out",
                str(out),
                "--query",
                "tilted-main",
                "--crash-at",
                "apply-before-commit:1",
            ]
        )
        self.assertEqual(crash.returncode, 86)
        resume = run_cli(
            [
                "resume",
                "--sample",
                str(self.sample),
                "--db",
                str(db),
                "--work",
                str(work),
                "--out",
                str(out),
                "--query",
                "tilted-main",
            ]
        )
        self.assertEqual(resume.returncode, 0, resume.stderr)
        report = json.loads(resume.stdout)["queries"][0]
        self.assertGreater(report["spill_and_memory"]["spill_count"], 0)
        self.assertEqual(self.sha(baseline), self.sha(out / "tilted-main.result.json"))

    def test_two_processes_stage_disjoint_batches(self) -> None:
        baseline = self.run_fresh("baseline3")
        db, work, out = self.paths("concurrent")
        common = [
            "run",
            "--sample",
            str(self.sample),
            "--db",
            str(db),
            "--work",
            str(work),
            "--out",
            str(out),
            "--query",
            "tilted-main",
            "--no-reset",
        ]
        env = {"PYTHONPATH": str(ROOT)}
        process_a = subprocess.Popen(
            [
                PYTHON,
                "-m",
                "gsb",
                *common,
                "--batch-index",
                "2",
                "--batch-index",
                "4",
                "--batch-index",
                "6",
                "--batch-index",
                "8",
                "--worker-id",
                "A",
            ],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**__import__("os").environ, **env},
        )
        process_b = subprocess.Popen(
            [
                PYTHON,
                "-m",
                "gsb",
                *common,
                "--batch-index",
                "1",
                "--batch-index",
                "3",
                "--batch-index",
                "5",
                "--batch-index",
                "7",
                "--worker-id",
                "B",
            ],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**__import__("os").environ, **env},
        )
        out_a, err_a = process_a.communicate(timeout=60)
        out_b, err_b = process_b.communicate(timeout=60)
        self.assertEqual(process_a.returncode, 0, err_a)
        self.assertEqual(process_b.returncode, 0, err_b)
        resume = run_cli(
            [
                "resume",
                "--sample",
                str(self.sample),
                "--db",
                str(db),
                "--work",
                str(work),
                "--out",
                str(out),
                "--query",
                "tilted-main",
            ]
        )
        self.assertEqual(resume.returncode, 0, resume.stderr)
        self.assertEqual(self.sha(baseline), self.sha(out / "tilted-main.result.json"))
        status = run_cli(["status", "--db", str(db), "--work", str(work)])
        query = json.loads(status.stdout)["queries"][0]
        self.assertEqual(query["watermark"]["applied_batch_count"], 8)
        verify = run_cli(
            [
                "verify",
                "--sample",
                str(self.sample),
                "--db",
                str(db),
                "--work",
                str(work),
                "--out",
                str(out),
                "--query",
                "tilted-main",
            ]
        )
        self.assertEqual(verify.returncode, 0, verify.stdout + verify.stderr)


if __name__ == "__main__":
    unittest.main()

