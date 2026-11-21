from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scheduler_diag.errors import BusinessError
from scheduler_diag.service import (
    load_workspace,
    schedule_workspace,
    verify_workspace,
)

from tests.helpers import (
    ROOT,
    SAMPLE_DIR,
    cli_json,
    run_cli,
    sample_copy,
    write_bundle,
)


class RecoveryTests(unittest.TestCase):
    def _load(self, database: Path) -> None:
        load_workspace(
            bundle_dir=SAMPLE_DIR,
            db_path=database,
            reset=True,
        )

    def test_crash_in_each_phase_recovers_without_duplicate_debits(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            clean_db = root / "clean.sqlite"
            self._load(clean_db)
            schedule_workspace(bundle_dir=SAMPLE_DIR, db_path=clean_db)
            clean_status = run_cli(
                "status",
                "--bundle",
                str(SAMPLE_DIR),
                "--db",
                str(clean_db),
                check=True,
            ).stdout
            for phase in ("filter", "score", "bind"):
                with self.subTest(phase=phase):
                    database = root / f"crash-{phase}.sqlite"
                    self._load(database)
                    crashed = run_cli(
                        "schedule",
                        "--bundle",
                        str(SAMPLE_DIR),
                        "--db",
                        str(database),
                        "--crash-at",
                        phase,
                    )
                    self.assertEqual(crashed.returncode, 97)
                    after_crash = cli_json(
                        "status",
                        "--bundle",
                        str(SAMPLE_DIR),
                        "--db",
                        str(database),
                    )
                    self.assertEqual(after_crash["ledger"]["version"], 0)
                    self.assertEqual(after_crash["round_watermark"], 0)
                    self.assertTrue(
                        all(pod["stage"] == "pending" for pod in after_crash["pods"])
                    )
                    self.assertEqual(
                        [
                            node["cpu_remaining"]
                            for node in after_crash["nodes"]
                        ],
                        [
                            node["cpu_total"]
                            for node in after_crash["nodes"]
                        ],
                    )
                    resumed = cli_json(
                        "resume",
                        "--bundle",
                        str(SAMPLE_DIR),
                        "--db",
                        str(database),
                    )
                    self.assertTrue(resumed["ok"])
                    self.assertEqual(resumed["pending_after"], 0)
                    verified = cli_json(
                        "verify",
                        "--bundle",
                        str(SAMPLE_DIR),
                        "--db",
                        str(database),
                    )
                    self.assertEqual(verified["checks"]["duplicate_debits"], 0)
                    self.assertEqual(verified["checks"]["dangling_debits"], 0)
                    resumed_status = run_cli(
                        "status",
                        "--bundle",
                        str(SAMPLE_DIR),
                        "--db",
                        str(database),
                        check=True,
                    ).stdout
                    self.assertEqual(resumed_status, clean_status)

    def test_repeated_schedule_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "state.sqlite"
            self._load(database)
            first = schedule_workspace(
                bundle_dir=SAMPLE_DIR,
                db_path=database,
            )
            second = schedule_workspace(
                bundle_dir=SAMPLE_DIR,
                db_path=database,
            )
            self.assertEqual(len(first["executed"]), 8)
            self.assertEqual(second["executed"], [])
            self.assertEqual(first["ledger"], second["ledger"])
            connection = sqlite3.connect(str(database))
            try:
                count = connection.execute(
                    "SELECT COUNT(*) FROM allocations"
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(count, 4)

    def test_two_concurrent_instances_commit_each_pod_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "state.sqlite"
            self._load(database)
            command = [
                sys.executable,
                "-m",
                "scheduler_diag",
                "schedule",
                "--bundle",
                str(SAMPLE_DIR),
                "--db",
                str(database),
            ]
            first = subprocess.Popen(
                command,
                cwd=str(ROOT),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            second = subprocess.Popen(
                command,
                cwd=str(ROOT),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            first_out, first_err = first.communicate(timeout=30)
            second_out, second_err = second.communicate(timeout=30)
            self.assertEqual(first.returncode, 0, first_err)
            self.assertEqual(second.returncode, 0, second_err)
            first_json = json.loads(first_out)
            second_json = json.loads(second_out)
            self.assertEqual(
                len(first_json["executed"]) + len(second_json["executed"]),
                8,
            )
            self.assertEqual(first_json["pending_after"], 0)
            self.assertEqual(second_json["pending_after"], 0)
            verified = verify_workspace(
                bundle_dir=SAMPLE_DIR,
                db_path=database,
            )
            self.assertEqual(verified["checks"]["allocations"], 4)
            self.assertEqual(verified["checks"]["duplicate_debits"], 0)

    def test_digest_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "state.sqlite"
            self._load(database)
            schedule_workspace(bundle_dir=SAMPLE_DIR, db_path=database)
            changed = sample_copy()
            changed["nodes"][0]["cpu_millicores"] += 1
            changed_dir = write_bundle(root / "changed", changed)
            with self.assertRaises(BusinessError) as caught:
                verify_workspace(
                    bundle_dir=changed_dir,
                    db_path=database,
                )
            self.assertEqual(caught.exception.code, "E_DIGEST_MISMATCH")

    def test_injection_watermark_tamper_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "state.sqlite"
            self._load(database)
            schedule_workspace(bundle_dir=SAMPLE_DIR, db_path=database)
            connection = sqlite3.connect(str(database))
            try:
                connection.execute(
                    "DELETE FROM injection_consumptions WHERE consumed_order = 4"
                )
                connection.commit()
            finally:
                connection.close()
            with self.assertRaises(BusinessError) as caught:
                verify_workspace(
                    bundle_dir=SAMPLE_DIR,
                    db_path=database,
                )
            self.assertEqual(caught.exception.code, "E_LEDGER_CORRUPT")

    def test_ledger_and_allocations_are_immutable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "state.sqlite"
            self._load(database)
            schedule_workspace(bundle_dir=SAMPLE_DIR, db_path=database)
            connection = sqlite3.connect(str(database))
            try:
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "UPDATE ledger_versions SET payload_json = '{}' WHERE version = 1"
                    )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "UPDATE allocations SET cpu_millicores = 1 WHERE pod = 'r1-web-control'"
                    )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "UPDATE pod_states SET terminal_status = 'tampered' "
                        "WHERE name = 'r1-web-control'"
                    )
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
