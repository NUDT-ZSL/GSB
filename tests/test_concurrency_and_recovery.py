from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.helpers import (
    ROOT,
    make_plan,
    record,
    run_cli,
    write_batch,
    write_manifest,
)


def build_case(root: Path, *, name: str) -> tuple[Path, Path, Path]:
    case = root / name
    case.mkdir()
    plan_path = make_plan(case, plan_id="recovery-test", budget_mib=0.004)
    batches = []
    seq = 0
    for batch_number in range(1, 5):
        rows = []
        for row_number in range(20):
            seq += 1
            group_number = (batch_number * 7 + row_number * 3) % 24
            rows.append(
                record(
                    seq,
                    10_000 + batch_number * 1000 + row_number,
                    src=f"lo:127.0.0.1:{41000 + group_number}",
                    dst=f"lo:127.0.0.1:{42000 + (group_number * 5) % 24}",
                    protocol=["TCP", "UDP", "QUIC"][group_number % 3],
                    direction=["ingress", "egress"][group_number % 2],
                    byte_count=50 + group_number * 3,
                    packets=1 + group_number % 5,
                )
            )
        batches.append(
            (
                f"b{batch_number:02d}",
                write_batch(case / f"b{batch_number:02d}.jsonl", rows),
            )
        )
    manifest = write_manifest(case / "manifest.json", batches)
    return plan_path, manifest, case


class RecoveryAndConcurrencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_crash_resume_matches_uninterrupted(self) -> None:
        clean_plan, clean_manifest, clean_case = build_case(self.root, name="clean")
        clean = run_cli(
            [
                "aggregate",
                "--plan",
                str(clean_plan),
                "--db",
                str(clean_case / "state.db"),
                "--manifest",
                str(clean_manifest),
            ]
        )
        self.assertEqual(clean.returncode, 0, clean.stderr)
        clean_verify = run_cli(
            [
                "verify",
                "--plan",
                str(clean_plan),
                "--db",
                str(clean_case / "state.db"),
                "--manifest",
                str(clean_manifest),
            ]
        )
        self.assertEqual(clean_verify.returncode, 0, clean_verify.stderr)
        clean_status = run_cli(
            [
                "status",
                "--plan",
                str(clean_plan),
                "--db",
                str(clean_case / "state.db"),
                "--with-groups",
            ]
        )
        self.assertEqual(clean_status.returncode, 0, clean_status.stderr)

        crash_plan, crash_manifest, crash_case = build_case(self.root, name="crash")
        crashed = run_cli(
            [
                "aggregate",
                "--plan",
                str(crash_plan),
                "--db",
                str(crash_case / "state.db"),
                "--manifest",
                str(crash_manifest),
            ],
            env={"FLOWAGG_FAULT_POINT": "after_spill"},
        )
        self.assertNotEqual(crashed.returncode, 0)
        pending = run_cli(
            [
                "status",
                "--plan",
                str(crash_plan),
                "--db",
                str(crash_case / "state.db"),
            ]
        )
        self.assertEqual(pending.returncode, 0, pending.stderr)
        self.assertEqual(
            json.loads(pending.stdout)["status"]["processing_batches"], 1
        )
        resumed = run_cli(
            [
                "resume",
                "--plan",
                str(crash_plan),
                "--db",
                str(crash_case / "state.db"),
                "--manifest",
                str(crash_manifest),
            ]
        )
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertGreater(
            json.loads(resumed.stdout)["orphans_removed"]["files"], 0
        )
        recovered_verify = run_cli(
            [
                "verify",
                "--plan",
                str(crash_plan),
                "--db",
                str(crash_case / "state.db"),
                "--manifest",
                str(crash_manifest),
            ]
        )
        self.assertEqual(recovered_verify.returncode, 0, recovered_verify.stderr)
        recovered_status = run_cli(
            [
                "status",
                "--plan",
                str(crash_plan),
                "--db",
                str(crash_case / "state.db"),
                "--with-groups",
            ]
        )
        self.assertEqual(recovered_status.returncode, 0, recovered_status.stderr)
        self.assertEqual(clean_verify.stdout, recovered_verify.stdout)
        self.assertEqual(clean_status.stdout, recovered_status.stdout)

    def test_parallel_same_batch_commits_once(self) -> None:
        case = self.root / "concurrent"
        case.mkdir()
        plan_path = make_plan(case, plan_id="concurrency-test", budget_mib=0.004)
        rows = [
            record(
                index,
                1000 + index,
                src=f"lo:127.0.0.1:{41000 + index}",
                dst=f"lo:127.0.0.1:{42000 + index}",
                byte_count=100 + index,
            )
            for index in range(1, 9)
        ]
        batch = write_batch(case / "b1.jsonl", rows)
        command = [
            sys.executable,
            "-m",
            "flowagg",
            "aggregate",
            "--plan",
            str(plan_path),
            "--db",
            str(case / "state.db"),
            "--input",
            str(batch),
            "--batch-id",
            "same",
        ]
        first = subprocess.Popen(
            command,
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        second = subprocess.Popen(
            command,
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        first_out, first_err = first.communicate(timeout=30)
        second_out, second_err = second.communicate(timeout=30)
        self.assertEqual(first.returncode, 0, first_err)
        self.assertEqual(second.returncode, 0, second_err)
        payloads = [json.loads(first_out), json.loads(second_out)]
        self.assertEqual(sum(bool(item.get("idempotent_replay")) for item in payloads), 1)
        status = run_cli(
            [
                "status",
                "--plan",
                str(plan_path),
                "--db",
                str(case / "state.db"),
            ]
        )
        self.assertEqual(status.returncode, 0, status.stderr)
        body = json.loads(status.stdout)["status"]
        self.assertEqual(body["committed_batches"], 1)
        self.assertEqual(body["group_count"], 8)
        self.assertEqual(body["consumed_record_seq"], 8)


if __name__ == "__main__":
    unittest.main()

