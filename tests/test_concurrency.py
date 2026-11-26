from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from seqledger.sample import generate_fixtures

from .helpers import ROOT, fixture_paths, merge_args, load_args, verify_args


class ConcurrencyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tempdir = tempfile.TemporaryDirectory()
        cls.fixture_dir = Path(cls.tempdir.name) / "fixtures"
        generate_fixtures(cls.fixture_dir)
        cls.paths = fixture_paths(cls.fixture_dir)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tempdir.cleanup()

    def _spawn(self, argv: list[str]) -> subprocess.Popen[str]:
        return subprocess.Popen(
            [sys.executable, "-m", "seqledger", *argv],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def test_parallel_replay_on_disjoint_partitions_matches_serial_verify(self) -> None:
        db = str(Path(self.tempdir.name) / "concurrent-replay.sqlite3")
        subprocess.run(
            [sys.executable, "-m", "seqledger", *load_args(self.paths, db=db)],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            [sys.executable, "-m", "seqledger", *merge_args(self.paths, db=db)],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        worker_a = self._spawn(
            [
                "--db",
                db,
                "replay",
                "--logical-clock-ms",
                "7000",
                "--partition",
                "orders/0",
                "--partition",
                "orders/1",
                "--partition",
                "orders/3",
            ]
        )
        worker_b = self._spawn(
            [
                "--db",
                db,
                "replay",
                "--logical-clock-ms",
                "7000",
                "--partition",
                "orders/2",
                "--partition",
                "payments/0",
                "--partition",
                "payments/1",
                "--partition",
                "payments/2",
            ]
        )
        stdout_a, stderr_a = worker_a.communicate(timeout=30)
        stdout_b, stderr_b = worker_b.communicate(timeout=30)
        self.assertEqual(worker_a.returncode, 0, stdout_a + stderr_a)
        self.assertEqual(worker_b.returncode, 0, stdout_b + stderr_b)
        status = json.loads(
            subprocess.run(
                [sys.executable, "-m", "seqledger", "--db", db, "status"],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
            ).stdout
        )
        self.assertEqual(status["totals"]["backlog_depth"], 0)
        self.assertEqual(status["totals"]["delivered_decision_count"], 18)
        self.assertEqual(status["totals"]["dead_letter_count"], 1)
        verified = json.loads(
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "seqledger",
                    *verify_args(self.paths, db=db),
                ],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
            ).stdout
        )
        self.assertTrue(verified["bitwise_match"])

    def test_concurrent_duplicate_loads_are_idempotent(self) -> None:
        db = str(Path(self.tempdir.name) / "concurrent-load.sqlite3")
        worker_a = self._spawn(load_args(self.paths, db=db))
        worker_b = self._spawn(load_args(self.paths, db=db))
        stdout_a, stderr_a = worker_a.communicate(timeout=30)
        stdout_b, stderr_b = worker_b.communicate(timeout=30)
        self.assertEqual(worker_a.returncode, 0, stdout_a + stderr_a)
        self.assertEqual(worker_b.returncode, 0, stdout_b + stderr_b)
        status = json.loads(
            subprocess.run(
                [sys.executable, "-m", "seqledger", "--db", db, "status"],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
            ).stdout
        )
        state = json.loads(
            subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "from seqledger.store import LedgerStore; "
                    f"s,_=LedgerStore({db!r}).read_state(); "
                    "import json; print(json.dumps(s))",
                ],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
            ).stdout
        )
        self.assertEqual(len(state["event_index"]), 18)
        self.assertEqual(status["partitions"][0]["committed_offset"], -1)

    def test_concurrent_duplicate_merge_replaces_pointer_once(self) -> None:
        db = str(Path(self.tempdir.name) / "concurrent-merge.sqlite3")
        subprocess.run(
            [sys.executable, "-m", "seqledger", *load_args(self.paths, db=db)],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        before = json.loads(
            subprocess.run(
                [sys.executable, "-m", "seqledger", "--db", db, "status"],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
            ).stdout
        )["ledger_version"]
        worker_a = self._spawn(merge_args(self.paths, db=db))
        worker_b = self._spawn(merge_args(self.paths, db=db))
        stdout_a, stderr_a = worker_a.communicate(timeout=30)
        stdout_b, stderr_b = worker_b.communicate(timeout=30)
        self.assertEqual(worker_a.returncode, 0, stdout_a + stderr_a)
        self.assertEqual(worker_b.returncode, 0, stdout_b + stderr_b)
        after = json.loads(
            subprocess.run(
                [sys.executable, "-m", "seqledger", "--db", db, "status"],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
            ).stdout
        )["ledger_version"]
        self.assertEqual(after, before + 1)

    def test_concurrent_duplicate_patch_is_idempotent(self) -> None:
        db = str(Path(self.tempdir.name) / "concurrent-patch.sqlite3")
        subprocess.run(
            [sys.executable, "-m", "seqledger", *load_args(self.paths, db=db)],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            [sys.executable, "-m", "seqledger", *merge_args(self.paths, db=db)],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        patch_args = [
            "--db",
            db,
            "patch",
            "--file",
            self.paths["patch"],
        ]
        worker_a = self._spawn(patch_args)
        worker_b = self._spawn(patch_args)
        stdout_a, stderr_a = worker_a.communicate(timeout=30)
        stdout_b, stderr_b = worker_b.communicate(timeout=30)
        self.assertEqual(worker_a.returncode, 0, stdout_a + stderr_a)
        self.assertEqual(worker_b.returncode, 0, stdout_b + stderr_b)
        payloads = [json.loads(stdout_a), json.loads(stdout_b)]
        self.assertEqual(sum(1 for payload in payloads if not payload["idempotent"]), 1)


if __name__ == "__main__":
    unittest.main()
