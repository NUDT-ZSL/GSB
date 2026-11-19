from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"


def scene_args() -> list[str]:
    return [
        "--link",
        str(SAMPLES / "link.json"),
        "--script",
        str(SAMPLES / "script.json"),
        "--congestion",
        str(SAMPLES / "congestion.json"),
        "--observations",
        str(SAMPLES / "observations.json"),
    ]


def command(*arguments: str) -> list[str]:
    return [sys.executable, "-m", "tcp_engine", *arguments]


def run_checked(*arguments: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        command(*arguments),
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )


class CliIntegrationTests(unittest.TestCase):
    def test_invalid_scene_never_creates_database(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            combined = json.loads((SAMPLES / "scene.json").read_text())
            combined["script"]["events"][2]["ts_ms"] = 1
            scene = root / "invalid-scene.json"
            scene.write_text(json.dumps(combined), encoding="utf-8")
            db = root / "must-not-exist.sqlite"
            failed = subprocess.run(
                command(
                    "run",
                    "--scene",
                    str(scene),
                    "--db",
                    str(db),
                    "--run-id",
                    "invalid",
                ),
                cwd=ROOT,
                text=True,
                capture_output=True,
            )
            self.assertEqual(failed.returncode, 2)
            self.assertEqual(
                json.loads(failed.stderr)["error"]["code"], "E_BAD_SCRIPT"
            )
            self.assertFalse(db.exists())

    def test_empty_script_finalizes_atomic_empty_trace(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            combined = json.loads((SAMPLES / "scene.json").read_text())
            combined["script"]["events"] = []
            scene = root / "empty-scene.json"
            scene.write_text(json.dumps(combined), encoding="utf-8")
            db = root / "empty.sqlite"
            result = json.loads(
                run_checked(
                    "run",
                    "--scene",
                    str(scene),
                    "--db",
                    str(db),
                    "--run-id",
                    "empty",
                ).stdout
            )
            self.assertTrue(result["complete"])
            self.assertEqual(result["committed_events"], 0)
            report = json.loads(
                run_checked(
                    "report", "--db", str(db), "--run-id", "empty"
                ).stdout
            )
            self.assertEqual(report["executed_event_count"], 0)
            self.assertEqual(report["cwnd_trace"], [])

    def test_two_runs_and_reports_are_byte_identical(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            outputs = []
            reports = []
            verifies = []
            for index in (1, 2):
                db = root / f"run-{index}.sqlite"
                result = run_checked(
                    "run",
                    *scene_args(),
                    "--db",
                    str(db),
                    "--run-id",
                    "demo",
                )
                outputs.append(result.stdout)
                self.assertTrue(json.loads(result.stdout)["complete"])
                report = run_checked(
                    "report", "--db", str(db), "--run-id", "demo"
                )
                reports.append(report.stdout)
                verify = run_checked(
                    "verify",
                    *scene_args(),
                    "--db",
                    str(db),
                    "--run-id",
                    "demo",
                )
                verifies.append(verify.stdout)
                self.assertTrue(json.loads(verify.stdout)["consistent"])
            self.assertEqual(outputs[0], outputs[1])
            self.assertEqual(reports[0], reports[1])
            self.assertEqual(verifies[0], verifies[1])

    def test_forced_termination_resume_skips_committed_events(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            crash_db = root / "crash.sqlite"
            clean_db = root / "clean.sqlite"
            env = dict(os.environ)
            env["TCP_ENGINE_CRASH_AFTER"] = "7"
            crashed = subprocess.run(
                command(
                    "run",
                    *scene_args(),
                    "--db",
                    str(crash_db),
                    "--run-id",
                    "demo",
                ),
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
            )
            self.assertEqual(crashed.returncode, 70)
            status = json.loads(
                run_checked(
                    "status", "--db", str(crash_db), "--run-id", "demo"
                ).stdout
            )
            self.assertIsNone(status["active"])
            self.assertEqual(status["latest"]["committed_events"], 7)
            self.assertEqual(status["latest"]["status"], "STAGING")

            resumed = json.loads(
                run_checked(
                    "resume",
                    *scene_args(),
                    "--db",
                    str(crash_db),
                    "--run-id",
                    "demo",
                ).stdout
            )
            self.assertEqual(resumed["committed_events"], 24)
            self.assertEqual(resumed["events_executed_this_call"], 17)
            self.assertTrue(resumed["complete"])

            clean = run_checked(
                "run",
                *scene_args(),
                "--db",
                str(clean_db),
                "--run-id",
                "demo",
            )
            self.assertEqual(
                resumed["trace_digest"], json.loads(clean.stdout)["trace_digest"]
            )
            report_crash = run_checked(
                "report", "--db", str(crash_db), "--run-id", "demo"
            ).stdout
            report_clean = run_checked(
                "report", "--db", str(clean_db), "--run-id", "demo"
            ).stdout
            self.assertEqual(report_crash, report_clean)

    def test_digest_mismatch_is_rejected_without_new_version(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = root / "state.sqlite"
            run_checked(
                "run",
                *scene_args(),
                "--db",
                str(db),
                "--run-id",
                "demo",
            )
            combined = json.loads((SAMPLES / "scene.json").read_text())
            combined["script"]["events"][2]["bytes"] += 1
            changed = root / "changed-scene.json"
            changed.write_text(json.dumps(combined), encoding="utf-8")
            failed = subprocess.run(
                command(
                    "run",
                    "--scene",
                    str(changed),
                    "--db",
                    str(db),
                    "--run-id",
                    "demo",
                ),
                cwd=ROOT,
                text=True,
                capture_output=True,
            )
            self.assertEqual(failed.returncode, 2)
            error = json.loads(failed.stderr)["error"]
            self.assertEqual(error["code"], "E_DIGEST_MISMATCH")
            status = json.loads(
                run_checked(
                    "status", "--db", str(db), "--run-id", "demo"
                ).stdout
            )
            self.assertEqual(status["active"]["version"], 1)
            self.assertEqual(status["latest"]["version"], 1)

    def test_parallel_segments_are_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = root / "parallel.sqlite"
            processes = []
            for start, end in ((0, 6), (6, 12), (12, 18), (18, 24)):
                processes.append(
                    subprocess.Popen(
                        command(
                            "run",
                            *scene_args(),
                            "--db",
                            str(db),
                            "--run-id",
                            "demo",
                            "--segment-start",
                            str(start),
                            "--segment-end",
                            str(end),
                            "--version-tag",
                            "parallel",
                        ),
                        cwd=ROOT,
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    )
                )
            for process in processes:
                stdout, stderr = process.communicate(timeout=60)
                self.assertEqual(process.returncode, 0, stderr)
                self.assertEqual(
                    json.loads(stdout)["source_digest"],
                    json.loads(
                        run_checked(
                            "status", "--db", str(db), "--run-id", "demo"
                        ).stdout
                    )["source_digest"],
                )
            final_status = json.loads(
                run_checked(
                    "status", "--db", str(db), "--run-id", "demo"
                ).stdout
            )
            self.assertEqual(final_status["latest"]["status"], "COMPLETE")
            self.assertEqual(final_status["latest"]["committed_events"], 24)
            self.assertIsNotNone(final_status["active"])
            duplicate = run_checked(
                "run",
                *scene_args(),
                "--db",
                str(db),
                "--run-id",
                "demo",
                "--segment-start",
                "0",
                "--segment-end",
                "6",
                "--version-tag",
                "parallel",
            )
            self.assertTrue(json.loads(duplicate.stdout)["complete"])
            verify = json.loads(
                run_checked(
                    "verify",
                    *scene_args(),
                    "--db",
                    str(db),
                    "--run-id",
                    "demo",
                ).stdout
            )
            self.assertEqual(verify["verified_events"], 24)
            self.assertTrue(verify["consistent"])

    def test_retry_exhaustion_returns_error_and_persists_failed_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            congestion = json.loads(
                (SAMPLES / "congestion.json").read_text(encoding="utf-8")
            )
            congestion["initial_rto_ms"] = 100
            congestion["max_rto_ms"] = 1000
            congestion["max_retries"] = 1
            script = {
                "initial_state": "ESTABLISHED",
                "events": [
                    {"event_id": "write", "ts_ms": 0, "type": "app_write", "bytes": 4000},
                    {"event_id": "rto-1", "ts_ms": 100, "type": "timeout", "seq": 0},
                    {"event_id": "rto-2", "ts_ms": 300, "type": "timeout", "seq": 0},
                ],
            }
            scene = {
                "link": json.loads((SAMPLES / "link.json").read_text()),
                "script": script,
                "congestion": congestion,
                "observations": json.loads(
                    (SAMPLES / "observations.json").read_text()
                ),
            }
            scene_path = root / "fail-scene.json"
            scene_path.write_text(json.dumps(scene), encoding="utf-8")
            db = root / "fail.sqlite"
            failed = subprocess.run(
                command(
                    "run",
                    "--scene",
                    str(scene_path),
                    "--db",
                    str(db),
                    "--run-id",
                    "fail",
                ),
                cwd=ROOT,
                text=True,
                capture_output=True,
            )
            self.assertEqual(failed.returncode, 2)
            self.assertEqual(
                json.loads(failed.stderr)["error"]["code"],
                "E_RETRY_EXHAUSTED",
            )
            status = json.loads(
                run_checked(
                    "status", "--db", str(db), "--run-id", "fail"
                ).stdout
            )
            self.assertEqual(status["active"]["status"], "FAILED")
            self.assertEqual(status["active"]["error_code"], "E_RETRY_EXHAUSTED")
            self.assertEqual(status["active"]["state"]["tcp_state"], "FAILED")
            report = json.loads(
                run_checked(
                    "report", "--db", str(db), "--run-id", "fail"
                ).stdout
            )
            self.assertEqual(report["failure"]["code"], "E_RETRY_EXHAUSTED")


if __name__ == "__main__":
    unittest.main()

