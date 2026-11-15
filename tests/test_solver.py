from __future__ import annotations

import json
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from level_solver.engine import LevelSolver, SolverInterrupted
from level_solver.errors import SolverError
from level_solver.grid import LevelState
from level_solver.model import digest_value, parse_spec
from level_solver.preflight import preflight
from level_solver.samples import conflict_spec_payload, demo_spec_payload
from level_solver.snapshots import SnapshotStore

REPO_ROOT = Path(__file__).resolve().parents[1]


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "level_solver", *args],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )


def write_spec(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def rehash_json(path: Path) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.pop("digest", None)
    payload["digest"] = digest_value(payload)
    path.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )


class SolverTests(unittest.TestCase):
    def test_invalid_spec_creates_no_snapshot_or_output(self) -> None:
        cases = [
            (
                {
                    "width": 0,
                    "height": 4,
                    "blocked": [],
                    "seed": 1,
                    "constraints": [],
                },
                "E_WIDTH",
            ),
            (
                {
                    "width": 4,
                    "height": 4,
                    "blocked": [[9, 9]],
                    "seed": 1,
                    "constraints": [],
                },
                "E_BLOCKED",
            ),
            (
                {
                    "width": 4,
                    "height": 4,
                    "blocked": [],
                    "seed": 1,
                    "constraints": [{"id": "x", "type": "not_a_constraint"}],
                },
                "E_CONSTRAINT_TYPE",
            ),
            (
                {
                    "width": 2,
                    "height": 1,
                    "blocked": [],
                    "seed": 1,
                    "constraints": [
                        {"id": "r", "type": "min_rooms", "count": 3}
                    ],
                },
                "E_CONSTRAINT_PARAM",
            ),
            (
                {
                    "width": 2,
                    "height": 1,
                    "blocked": [],
                    "seed": 1,
                },
                "E_CONSTRAINT_PARAM",
            ),
        ]
        for index, (payload, expected_code) in enumerate(cases):
            with self.subTest(index=index, code=expected_code):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    spec_path = root / "spec.json"
                    snapshot_dir = root / "snapshots"
                    output_path = root / "result.json"
                    write_spec(spec_path, payload)
                    completed = run_cli(
                        "solve",
                        str(spec_path),
                        "--snapshot-dir",
                        str(snapshot_dir),
                        "--output",
                        str(output_path),
                    )
                    self.assertEqual(completed.returncode, 2)
                    result = json.loads(completed.stdout)
                    self.assertEqual(result["error"]["code"], expected_code)
                    self.assertFalse(snapshot_dir.exists())
                    self.assertFalse(output_path.exists())

    def test_memory_budget_rejected_before_snapshot(self) -> None:
        payload = demo_spec_payload()
        payload["memory_budget_kb"] = 1
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spec_path = root / "spec.json"
            output_path = root / "result.json"
            write_spec(spec_path, payload)
            completed = run_cli(
                "solve",
                str(spec_path),
                "--snapshot-dir",
                str(root / "snapshots"),
                "--output",
                str(output_path),
            )
            self.assertEqual(completed.returncode, 2)
            result = json.loads(completed.stdout)
            self.assertEqual(result["error"]["code"], "E_RESOURCE")
            self.assertFalse((root / "snapshots").exists())
            self.assertFalse(output_path.exists())

    def test_deterministic_repeat(self) -> None:
        payload = demo_spec_payload()
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            result_a = LevelSolver(
                parse_spec(payload), Path(first)
            ).solve().as_dict()
            result_b = LevelSolver(
                parse_spec(payload), Path(second)
            ).solve().as_dict()
        self.assertEqual(result_a["grid"], result_b["grid"])
        self.assertEqual(result_a["rooms"], result_b["rooms"])
        self.assertEqual(result_a["version_id"], result_b["version_id"])
        self.assertEqual(
            result_a["candidate_attempts"], result_b["candidate_attempts"]
        )
        self.assertEqual(
            result_a["constraint_application_count"],
            result_b["constraint_application_count"],
        )
        self.assertEqual(
            [item["digest"] for item in result_a["snapshot_versions"]],
            [item["digest"] for item in result_b["snapshot_versions"]],
        )

    def test_concurrency_does_not_change_selection(self) -> None:
        serial_payload = demo_spec_payload()
        parallel_payload = demo_spec_payload()
        serial_payload["concurrency"] = 1
        parallel_payload["concurrency"] = 4
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            serial = LevelSolver(
                parse_spec(serial_payload), Path(first)
            ).solve().as_dict()
            parallel = LevelSolver(
                parse_spec(parallel_payload), Path(second)
            ).solve().as_dict()
        self.assertEqual(serial["grid"], parallel["grid"])
        self.assertEqual(serial["rooms"], parallel["rooms"])
        self.assertEqual(serial["candidate_attempts"], parallel["candidate_attempts"])
        self.assertEqual(serial["rollback_count"], parallel["rollback_count"])
        self.assertLessEqual(
            parallel["preflight"]["preflight_workers_max"], 4
        )

    def test_interrupt_and_resume_match_uninterrupted(self) -> None:
        payload = demo_spec_payload()
        spec = parse_spec(payload)
        with tempfile.TemporaryDirectory() as resumable, tempfile.TemporaryDirectory() as baseline:
            with self.assertRaises(SolverInterrupted) as caught:
                LevelSolver(
                    spec, Path(resumable), interrupt_after=2
                ).solve()
            self.assertEqual(caught.exception.exit_code, 75)
            self.assertTrue(caught.exception.marker_path.exists())
            resumed = LevelSolver(spec, Path(resumable)).solve().as_dict()
            marker_removed = not (Path(resumable) / "interrupt.json").exists()
            expected = LevelSolver(spec, Path(baseline)).solve().as_dict()
        self.assertEqual(resumed["resume"]["source"], "interrupt_marker")
        self.assertEqual(resumed["resume"]["start_index"], 2)
        self.assertEqual(resumed["grid"], expected["grid"])
        self.assertEqual(resumed["version_id"], expected["version_id"])
        self.assertEqual(
            resumed["candidate_attempts"], expected["candidate_attempts"]
        )
        self.assertTrue(marker_removed)

    def test_force_resume_from_latest_snapshot_without_marker(self) -> None:
        payload = demo_spec_payload()
        spec = parse_spec(payload)
        with tempfile.TemporaryDirectory() as resumable, tempfile.TemporaryDirectory() as baseline:
            with self.assertRaises(SolverInterrupted):
                LevelSolver(
                    spec, Path(resumable), interrupt_after=2
                ).solve()
            marker = Path(resumable) / "interrupt.json"
            marker.unlink()
            resumed = LevelSolver(spec, Path(resumable)).solve().as_dict()
            expected = LevelSolver(spec, Path(baseline)).solve().as_dict()
        self.assertEqual(
            resumed["resume"]["source"], "latest_snapshot_no_marker"
        )
        self.assertEqual(resumed["grid"], expected["grid"])
        self.assertEqual(resumed["version_id"], expected["version_id"])

    def test_actual_sigint_sets_marker_and_exit_75(self) -> None:
        payload = demo_spec_payload()
        with tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryDirectory() as baseline:
            root = Path(temporary)
            spec_path = root / "spec.json"
            snapshots = root / "snapshots"
            write_spec(spec_path, payload)
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "level_solver",
                    "solve",
                    str(spec_path),
                    "--snapshot-dir",
                    str(snapshots),
                    "--step-delay-ms",
                    "200",
                ],
                cwd=REPO_ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            deadline = time.time() + 5
            while time.time() < deadline:
                if (snapshots / "manifest.json").exists():
                    break
                time.sleep(0.02)
            time.sleep(0.25)
            process.send_signal(signal.SIGINT)
            stdout, stderr = process.communicate(timeout=15)
            self.assertEqual(process.returncode, 75, stderr)
            payload_out = json.loads(stdout)
            self.assertEqual(payload_out["error"]["code"], "E_INTERRUPTED")
            self.assertTrue((snapshots / "interrupt.json").exists())
            resumed = LevelSolver(
                parse_spec(payload), snapshots
            ).solve().as_dict()
            expected = LevelSolver(
                parse_spec(payload), Path(baseline)
            ).solve().as_dict()
        self.assertEqual(resumed["grid"], expected["grid"])
        self.assertEqual(resumed["version_id"], expected["version_id"])

    def test_tampered_latest_snapshot_falls_back(self) -> None:
        payload = demo_spec_payload()
        spec = parse_spec(payload)
        with tempfile.TemporaryDirectory() as damaged, tempfile.TemporaryDirectory() as baseline:
            LevelSolver(spec, Path(damaged)).solve()
            manifest = json.loads(
                (Path(damaged) / "manifest.json").read_text(encoding="utf-8")
            )
            latest = Path(damaged) / "snapshots" / manifest["latest_snapshot"]
            snapshot = json.loads(latest.read_text(encoding="utf-8"))
            snapshot["grid"][2][2] = 2
            latest.write_text(
                json.dumps(snapshot, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )

            with self.assertRaises(SolverError) as verify_error:
                SnapshotStore(Path(damaged), spec).verify()
            self.assertEqual(verify_error.exception.code, "E_DIGEST")

            with self.assertRaises(SolverError) as strict_error:
                LevelSolver(spec, Path(damaged), strict_digest=True).solve()
            self.assertEqual(strict_error.exception.code, "E_DIGEST")

            resumed = LevelSolver(spec, Path(damaged)).solve().as_dict()
            corrupt_archived = bool(
                list((Path(damaged) / "archive" / "corrupt").glob("*.json"))
            )
            expected = LevelSolver(spec, Path(baseline)).solve().as_dict()
        self.assertEqual(resumed["grid"], expected["grid"])
        self.assertEqual(resumed["version_id"], expected["version_id"])
        self.assertIn(
            "E_DIGEST", {warning["code"] for warning in resumed["warnings"]}
        )
        self.assertEqual(resumed["resume"]["start_index"], 4)
        self.assertTrue(corrupt_archived)

    def test_missing_snapshot_digest_is_rejected(self) -> None:
        payload = demo_spec_payload()
        spec = parse_spec(payload)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            LevelSolver(spec, root).solve()
            manifest = json.loads(
                (root / "manifest.json").read_text(encoding="utf-8")
            )
            latest = root / "snapshots" / manifest["latest_snapshot"]
            snapshot = json.loads(latest.read_text(encoding="utf-8"))
            snapshot.pop("digest")
            latest.write_text(json.dumps(snapshot), encoding="utf-8")
            with self.assertRaises(SolverError) as caught:
                SnapshotStore(root, spec).verify()
            self.assertEqual(caught.exception.code, "E_DIGEST")
            with self.assertRaises(SolverError) as strict:
                LevelSolver(spec, root, strict_digest=True).solve()
            self.assertEqual(strict.exception.code, "E_DIGEST")

    def test_manifest_metadata_tamper_is_rejected(self) -> None:
        payload = demo_spec_payload()
        spec = parse_spec(payload)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            LevelSolver(spec, root).solve()
            manifest_path = root / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["applied_count"] += 1
            manifest_path.write_text(
                json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            rehash_json(manifest_path)
            with self.assertRaises(SolverError) as caught:
                SnapshotStore(root, spec).verify()
            self.assertEqual(caught.exception.code, "E_DIGEST")
            with self.assertRaises(SolverError) as strict:
                LevelSolver(spec, root, strict_digest=True).solve()
            self.assertEqual(strict.exception.code, "E_DIGEST")

    def test_stale_marker_version_restarts_from_scratch(self) -> None:
        payload = demo_spec_payload()
        spec = parse_spec(payload)
        with tempfile.TemporaryDirectory() as resumable, tempfile.TemporaryDirectory() as baseline:
            with self.assertRaises(SolverInterrupted):
                LevelSolver(
                    spec, Path(resumable), interrupt_after=2
                ).solve()
            marker_path = Path(resumable) / "interrupt.json"
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            marker["latest_version_id"] = "0" * 64
            marker_path.write_text(
                json.dumps(marker, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            rehash_json(marker_path)
            resumed = LevelSolver(spec, Path(resumable)).solve().as_dict()
            expected = LevelSolver(spec, Path(baseline)).solve().as_dict()
        self.assertEqual(
            resumed["resume"]["source"],
            "fresh_after_stale_marker_version",
        )
        self.assertTrue(resumed["resume"]["restarted"])
        self.assertEqual(resumed["grid"], expected["grid"])
        self.assertEqual(resumed["version_id"], expected["version_id"])

    def test_rollback_uses_previous_constraint_alternative(self) -> None:
        payload = {
            "width": 10,
            "height": 4,
            "blocked": [[5, y] for y in range(4)],
            "seed": 2,
            "rollback_limit": 8,
            "attempt_limit": 12,
            "concurrency": 4,
            "memory_budget_kb": 256,
            "constraints": [
                {"id": "rooms", "type": "min_rooms", "count": 2},
                {"id": "conn", "type": "connectivity"},
            ],
        }
        spec = parse_spec(payload)
        with tempfile.TemporaryDirectory() as resumable, tempfile.TemporaryDirectory() as baseline:
            with self.assertRaises(SolverInterrupted):
                LevelSolver(
                    spec, Path(resumable), interrupt_after=1
                ).solve()
            resumed = LevelSolver(spec, Path(resumable)).solve().as_dict()
            expected = LevelSolver(spec, Path(baseline)).solve().as_dict()
        self.assertGreater(expected["rollback_count"], 0)
        self.assertIn(
            {"earlier": "rooms", "later": "conn"}, expected["conflicts"]
        )
        self.assertEqual(resumed["grid"], expected["grid"])
        self.assertEqual(resumed["version_id"], expected["version_id"])
        self.assertEqual(
            resumed["candidate_attempts"], expected["candidate_attempts"]
        )

    def test_crash_window_recovers_newer_uncommitted_snapshot(self) -> None:
        payload = {
            "width": 10,
            "height": 4,
            "blocked": [[5, y] for y in range(4)],
            "seed": 2,
            "rollback_limit": 8,
            "attempt_limit": 12,
            "concurrency": 4,
            "memory_budget_kb": 256,
            "constraints": [
                {"id": "rooms", "type": "min_rooms", "count": 2},
                {"id": "conn", "type": "connectivity"},
            ],
        }
        spec = parse_spec(payload)
        with tempfile.TemporaryDirectory() as working, tempfile.TemporaryDirectory() as baseline:
            root = Path(working)
            LevelSolver(spec, root).solve()
            expected = LevelSolver(spec, Path(baseline)).solve().as_dict()

            snapshots = {}
            for path in (root / "snapshots").glob("step-*.json"):
                value = json.loads(path.read_text(encoding="utf-8"))
                snapshots.setdefault(value["applied_count"], []).append((path, value))
            old_path = next(
                (root / "archive" / "superseded").glob("step-000001-*.json")
            )
            old_value = json.loads(old_path.read_text(encoding="utf-8"))

            # Simulate a kill after the replacement step-1 snapshot was
            # atomically written but before manifest update and step 2.
            shutil.copy2(old_path, root / "snapshots" / old_path.name)
            for path, _value in snapshots.get(2, []):
                path.unlink()
            manifest = {
                "format_version": 1,
                "run_id": spec.run_id,
                "spec_digest": spec.spec_digest,
                "seed": spec.seed,
                "status": "running",
                "latest_snapshot": old_path.name,
                "latest_version_id": old_value["version_id"],
                "latest_snapshot_digest": old_value["digest"],
                "applied_count": 1,
                "commit_sequence": old_value["commit_sequence"],
                "rollback_count": 0,
                "attempts_total": 1,
                "superseded_versions": [],
                "unresolved_constraints": [],
            }
            manifest["digest"] = digest_value(manifest)
            (root / "manifest.json").write_text(
                json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            resumed = LevelSolver(spec, root).solve().as_dict()
        self.assertEqual(
            resumed["resume"]["source"], "latest_snapshot_no_marker"
        )
        self.assertEqual(resumed["grid"], expected["grid"])
        self.assertEqual(resumed["version_id"], expected["version_id"])
        self.assertEqual(
            resumed["candidate_attempts"], expected["candidate_attempts"]
        )

    def test_conflict_returns_e_conflict_without_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spec_path = root / "spec.json"
            snapshot_dir = root / "snapshots"
            output_path = root / "result.json"
            write_spec(spec_path, conflict_spec_payload())
            completed = run_cli(
                "solve",
                str(spec_path),
                "--snapshot-dir",
                str(snapshot_dir),
                "--output",
                str(output_path),
            )
            self.assertEqual(completed.returncode, 3)
            result = json.loads(completed.stdout)
            self.assertEqual(result["error"]["code"], "E_CONFLICT")
            details = result["error"]["details"]
            self.assertEqual(details["unresolved_constraints"], ["spacing"])
            self.assertIn(
                {"earlier": "rooms", "later": "spacing"},
                details["conflict_pairs"],
            )
            self.assertFalse(output_path.exists())

    def test_new_run_archives_changed_versions(self) -> None:
        first_payload = demo_spec_payload()
        second_payload = demo_spec_payload()
        second_payload["seed"] += 1
        with tempfile.TemporaryDirectory() as shared, tempfile.TemporaryDirectory() as clean:
            first = LevelSolver(
                parse_spec(first_payload), Path(shared)
            ).solve().as_dict()
            old_latest = first["version_id"]
            second = LevelSolver(
                parse_spec(second_payload), Path(shared)
            ).solve().as_dict()
            expected = LevelSolver(
                parse_spec(second_payload), Path(clean)
            ).solve().as_dict()
            archived_versions = {
                json.loads(path.read_text(encoding="utf-8")).get("version_id")
                for path in (Path(shared) / "archive" / "runs").rglob("*.json")
            }
        self.assertNotEqual(second["version_id"], old_latest)
        self.assertIn(old_latest, archived_versions)
        self.assertEqual(second["grid"], expected["grid"])

    def test_preflight_preserves_serial_first_error(self) -> None:
        payload = {
            "width": 2,
            "height": 1,
            "blocked": [],
            "seed": 4,
            "rollback_limit": 0,
            "attempt_limit": 1,
            "concurrency": 4,
            "memory_budget_kb": 64,
            "constraints": [
                {"id": "rooms", "type": "min_rooms", "count": 2},
                {"id": "spacing", "type": "room_spacing", "min_spacing": 1},
            ],
        }
        spec = parse_spec(payload)
        state = LevelState.initial(spec.width, spec.height, spec.blocked)
        serial_payload = dict(payload)
        serial_payload["concurrency"] = 1
        serial_spec = parse_spec(serial_payload)
        parallel_report = preflight(spec, state, spec.constraints)
        serial_report = preflight(serial_spec, state, serial_spec.constraints)
        self.assertFalse(parallel_report.ok)
        self.assertEqual(
            parallel_report.errors[0].constraint_id,
            serial_report.errors[0].constraint_id,
        )
        self.assertEqual(parallel_report.errors[0].constraint_id, "rooms")
        self.assertLessEqual(parallel_report.workers, 4)
        self.assertEqual(parallel_report.max_concurrent_states, 1)
        self.assertGreaterEqual(parallel_report.state_limit_by_memory, 2)


if __name__ == "__main__":
    unittest.main()
