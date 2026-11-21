from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

from ledger_engine.engine import _metric_payload
from ledger_engine.jsonutil import canonical_json
from ledger_engine.numeric import FixedValue
from ledger_engine.orchestration import load_sample


ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "samples" / "default"


def run_cli(
    *args: str,
    env: dict[str, str] | None = None,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, "-m", "ledger_engine", *args]
    process = subprocess.run(
        command,
        cwd=ROOT,
        env={**os.environ, **(env or {})},
        text=True,
        capture_output=True,
    )
    if check and process.returncode != 0:
        raise AssertionError(
            f"command failed ({process.returncode}): {' '.join(command)}\n"
            f"stdout:\n{process.stdout}\nstderr:\n{process.stderr}"
        )
    return process


def parse(process: subprocess.CompletedProcess[str]) -> dict:
    return json.loads(process.stdout)


class ValidationTests(unittest.TestCase):
    def test_cycle_writes_no_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shutil.copy2(SAMPLE / "layers.json", root / "layers.json")
            shutil.copy2(SAMPLE / "ledger_config.json", root / "ledger_config.json")
            (root / "topology.json").write_text(
                json.dumps(
                    {
                        "services": [
                            {"name": "a", "layer": "edge", "depends_on": ["b"]},
                            {"name": "b", "layer": "edge", "depends_on": ["a"]},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            db = root / "state.sqlite3"
            process = run_cli("--db", str(db), "--sample", str(root), "load")
            self.assertEqual(process.returncode, 2)
            self.assertEqual(parse(process)["error"]["code"], "E_CYCLE")
            self.assertFalse(db.exists())
            self.assertFalse(Path(str(db) + ".lock").exists())

    def test_bad_scale_writes_no_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shutil.copy2(SAMPLE / "layers.json", root / "layers.json")
            shutil.copy2(SAMPLE / "topology.json", root / "topology.json")
            config = json.loads(
                (SAMPLE / "ledger_config.json").read_text(encoding="utf-8")
            )
            config["param_scales"]["timeout_ms"] = 19
            (root / "ledger_config.json").write_text(
                json.dumps(config), encoding="utf-8"
            )
            db = root / "state.sqlite3"
            process = run_cli("--db", str(db), "--sample", str(root), "load")
            self.assertEqual(process.returncode, 2)
            self.assertEqual(parse(process)["error"]["code"], "E_BAD_SCALE")
            self.assertFalse(db.exists())

    def test_bad_patch_writes_no_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            patch = root / "bad.json"
            patch.write_text(
                json.dumps(
                    {
                        "operations": [
                            {
                                "layer": "missing",
                                "op": "set_param",
                                "parameter": "timeout_ms",
                                "value": 1,
                            }
                        ],
                        "patch_id": "bad-layer",
                    }
                ),
                encoding="utf-8",
            )
            db = root / "state.sqlite3"
            process = run_cli(
                "--db",
                str(db),
                "--sample",
                str(SAMPLE),
                "diagnose",
                "--patch",
                str(patch),
            )
            self.assertEqual(process.returncode, 2)
            self.assertEqual(parse(process)["error"]["code"], "E_BAD_PATCH")
            self.assertFalse(db.exists())


class DeterminismTests(unittest.TestCase):
    def test_two_fresh_runs_are_byte_identical(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            outputs = []
            for run_name in ("a", "b"):
                run_dir = root / run_name
                run_dir.mkdir()
                db = run_dir / "ledger.sqlite3"
                commands = [
                    ("load",),
                    ("diagnose", "--workers", "4"),
                    (
                        "diagnose",
                        "--patch",
                        str(SAMPLE / "patches" / "01-api-a-retry.json"),
                    ),
                    ("status",),
                    ("verify",),
                ]
                texts = []
                for command in commands:
                    process = run_cli(
                        "--db",
                        str(db),
                        "--sample",
                        str(SAMPLE),
                        *command,
                        check=True,
                    )
                    texts.append(process.stdout)
                outputs.append(texts)
            self.assertEqual(outputs[0], outputs[1])
            verify = json.loads(outputs[0][-1])
            self.assertTrue(verify["ok"])
            self.assertEqual(verify["service_count"], 8)
            self.assertGreater(verify["ledger_entries"], 0)

    def test_repeated_same_diagnose_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            db = Path(temporary) / "ledger.sqlite3"
            args = ("--db", str(db), "--sample", str(SAMPLE), "diagnose")
            first = run_cli(*args, check=True)
            second = run_cli(*args, check=True)
            self.assertEqual(first.stdout, second.stdout)
            with sqlite3.connect(db) as connection:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM rounds").fetchone()[0],
                    1,
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM service_results"
                    ).fetchone()[0],
                    8,
                )


class PatchAndRecoveryTests(unittest.TestCase):
    def test_patch_closure_reuse_and_delete_restore(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            db = Path(temporary) / "ledger.sqlite3"
            run_cli(
                "--db",
                str(db),
                "--sample",
                str(SAMPLE),
                "diagnose",
                check=True,
            )
            base_status = parse(
                run_cli("--db", str(db), "status", check=True)
            )
            p1 = parse(
                run_cli(
                    "--db",
                    str(db),
                    "--sample",
                    str(SAMPLE),
                    "diagnose",
                    "--patch",
                    str(SAMPLE / "patches" / "01-api-a-retry.json"),
                    check=True,
                )
            )
            self.assertEqual(
                p1["affected_services"], ["api-a", "edge-gateway", "edge-shadow"]
            )
            status_p1 = parse(
                run_cli("--db", str(db), "status", check=True)
            )
            base_by_name = {
                service["service"]: service for service in base_status["services"]
            }
            p1_by_name = {
                service["service"]: service for service in status_p1["services"]
            }
            for name in ("db-primary", "db-replica", "api-b", "worker-a", "worker-b"):
                self.assertEqual(
                    base_by_name[name]["derived"],
                    p1_by_name[name]["derived"],
                )
            decision = status_p1["invalidations"]["decisions"]["api-a"]
            self.assertEqual(
                decision["recomputed_conversions"], ["retry_multiplier"]
            )
            self.assertIn("timeout_ms", decision["reused_conversions"])
            with sqlite3.connect(db) as connection:
                row = connection.execute(
                    "SELECT entry_json FROM ledger_entries "
                    "WHERE round_id = ? AND service = 'api-a' "
                    "AND registration_key = 'convert:retry_multiplier'",
                    (status_p1["round_id"],),
                ).fetchone()
                self.assertIsNotNone(row)
                entry = json.loads(row[0])
                self.assertEqual(entry["carry_in"], "1/2")
                self.assertEqual(entry["output_units"], 1333335)
                self.assertEqual(entry["discarded_units"], "0/1")

            run_cli(
                "--db",
                str(db),
                "--sample",
                str(SAMPLE),
                "diagnose",
                "--patch",
                str(SAMPLE / "patches" / "02-db-primary-capacity.json"),
                check=True,
            )
            p3 = parse(
                run_cli(
                    "--db",
                    str(db),
                    "--sample",
                    str(SAMPLE),
                    "diagnose",
                    "--patch",
                    str(SAMPLE / "patches" / "03-delete-api-a-retry.json"),
                    check=True,
                )
            )
            self.assertEqual(
                p3["trigger_sources"], ["layers.api_a.retry_multiplier"]
            )
            verify = parse(run_cli("--db", str(db), "verify", check=True))
            self.assertTrue(verify["ok"])
            status_p3 = parse(run_cli("--db", str(db), "status", check=True))
            with sqlite3.connect(db) as connection:
                entry = json.loads(
                    connection.execute(
                        "SELECT entry_json FROM ledger_entries "
                        "WHERE round_id = ? AND service = 'api-a' "
                        "AND registration_key = 'convert:retry_multiplier'",
                        (status_p3["round_id"],),
                    ).fetchone()[0]
                )
            self.assertEqual(entry["input"], "1.3333335")

    def test_forced_termination_resume_does_not_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db = root / "ledger.sqlite3"
            run_cli(
                "--db", str(db), "--sample", str(SAMPLE), "load", check=True
            )
            crashed = run_cli(
                "--db",
                str(db),
                "--sample",
                str(SAMPLE),
                "diagnose",
                "--workers",
                "2",
                env={"LEDGER_CRASH_AFTER_STAGED_SERVICE": "3"},
            )
            self.assertEqual(crashed.returncode, 137)
            status = parse(run_cli("--db", str(db), "status", check=True))
            self.assertFalse(status["complete"])
            self.assertIsNotNone(status["pending_round"])
            self.assertGreater(len(status["services"]), 0)
            with sqlite3.connect(db) as connection:
                staged_before = connection.execute(
                    "SELECT COUNT(*) FROM ledger_entries WHERE status = 'STAGED'"
                ).fetchone()[0]
                entries_before = connection.execute(
                    "SELECT COUNT(*) FROM ledger_entries"
                ).fetchone()[0]
                staged_keys = [
                    row[0]
                    for row in connection.execute(
                        "SELECT service || ':' || registration_key "
                        "FROM ledger_entries WHERE status = 'STAGED' "
                        "ORDER BY service, registration_no"
                    ).fetchall()
                ]
            self.assertGreater(staged_before, 0)
            resumed = parse(
                run_cli(
                    "--db",
                    str(db),
                    "--sample",
                    str(SAMPLE),
                    "resume",
                    check=True,
                )
            )
            self.assertTrue(resumed["resumed"])
            verify = parse(run_cli("--db", str(db), "verify", check=True))
            self.assertTrue(verify["ok"])
            with sqlite3.connect(db) as connection:
                staged_after = connection.execute(
                    "SELECT COUNT(*) FROM ledger_entries WHERE status = 'STAGED'"
                ).fetchone()[0]
                entries_after = connection.execute(
                    "SELECT COUNT(*) FROM ledger_entries"
                ).fetchone()[0]
                duplicates = connection.execute(
                    "SELECT COUNT(*) FROM ("
                    "SELECT round_id, service, registration_no, COUNT(*) AS n "
                    "FROM ledger_entries GROUP BY 1, 2, 3 HAVING n > 1)"
                ).fetchone()[0]
                staged_counts = {
                    row[0]: row[1]
                    for row in connection.execute(
                        "SELECT service || ':' || registration_key, COUNT(*) "
                        "FROM ledger_entries GROUP BY 1"
                    ).fetchall()
                }
            self.assertEqual(staged_after, 0)
            self.assertGreaterEqual(entries_after, entries_before)
            self.assertTrue(staged_keys)
            for key in staged_keys:
                self.assertEqual(staged_counts[key], 1)
            self.assertEqual(duplicates, 0)

    def test_concurrent_instances_commit_one_round(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            db = Path(temporary) / "ledger.sqlite3"
            run_cli(
                "--db", str(db), "--sample", str(SAMPLE), "load", check=True
            )
            processes = [
                subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "ledger_engine",
                        "--db",
                        str(db),
                        "--sample",
                        str(SAMPLE),
                        "diagnose",
                        "--workers",
                        "2",
                    ],
                    cwd=ROOT,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                for _ in range(4)
            ]
            outputs = []
            for process in processes:
                stdout, stderr = process.communicate(timeout=60)
                self.assertEqual(process.returncode, 0, stderr)
                outputs.append(stdout)
            self.assertEqual(len(set(outputs)), 1)
            with sqlite3.connect(db) as connection:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM rounds").fetchone()[0],
                    1,
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM service_results"
                    ).fetchone()[0],
                    8,
                )
            verify = parse(run_cli("--db", str(db), "verify", check=True))
            self.assertTrue(verify["ok"])


class DefectTests(unittest.TestCase):
    def test_unexplained_deviation_is_flagged(self) -> None:
        config = load_sample(SAMPLE).config
        source = {
            "bound_after": "0.001",
            "operation": "CONVERT",
            "parameter": "timeout_ms",
            "registration_no": 1,
            "service": "s",
        }
        payload = _metric_payload(
            fixed_value=FixedValue(
                units=1000,
                scale=3,
                low=Fraction(1),
                high=Fraction(1),
            ),
            truth_value=Decimal("2"),
            sources=[source],
            config=config,
        )
        self.assertFalse(payload["ledger_ok"])
        self.assertIsNotNone(payload["first_defect"])
        self.assertEqual(payload["first_defect"]["registration_no"], 1)

    def test_verify_detects_tampered_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            db = Path(temporary) / "ledger.sqlite3"
            run_cli(
                "--db", str(db), "--sample", str(SAMPLE), "diagnose", check=True
            )
            with sqlite3.connect(db) as connection:
                row = connection.execute(
                    "SELECT round_id, service, registration_no, entry_json "
                    "FROM ledger_entries ORDER BY service, registration_no LIMIT 1"
                ).fetchone()
                entry = json.loads(row[3])
                entry["output_units"] = int(entry["output_units"]) + 1
                from ledger_engine.jsonutil import hash_json

                connection.execute(
                    "UPDATE ledger_entries SET entry_json = ?, entry_hash = ? "
                    "WHERE round_id = ? AND service = ? AND registration_no = ?",
                    (
                        canonical_json(entry),
                        hash_json(entry),
                        row[0],
                        row[1],
                        row[2],
                    ),
                )
            process = run_cli("--db", str(db), "verify")
            self.assertEqual(process.returncode, 1)
            document = parse(process)
            self.assertFalse(document["ok"])
            codes = {error["code"] for error in document["errors"]}
            self.assertTrue(
                {"E_LEDGER_ARITHMETIC", "E_SOURCE_MISMATCH"} & codes,
                codes,
            )


if __name__ == "__main__":
    unittest.main()
