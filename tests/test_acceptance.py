from __future__ import annotations

import copy
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from animrecalc.cli import main as cli_main
from animrecalc.edits import parse_edit_script
from animrecalc.engine import AnimEngine
from animrecalc.evaluator import build_state_trace, evaluate_full_pose
from animrecalc.pipeline import normalize_project, project_version
from animrecalc.sample_gen import build_samples, generate_sample_tree
from animrecalc.scheduler import BoundedChunkScheduler
from animrecalc.storage import ChunkStore


class SampleMixin:
    def make_project(self, tmp: Path):
        samples = build_samples()
        paths = generate_sample_tree(tmp / "samples")
        project = normalize_project(
            samples["skeleton"],
            samples["clips"],
            samples["state_machine"],
            samples["chunk_spec"],
        )
        edits = parse_edit_script(samples["edit_script"])
        return project, edits, paths


class ValidationTests(unittest.TestCase):
    def test_business_codes_and_no_output_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            paths = generate_sample_tree(tmp / "samples")
            bad_skeleton = tmp / "bad_skeleton.json"
            bad_skeleton.write_text(
                json.dumps(
                    {
                        "bones": [
                            {"name": "a", "parent": "b"},
                            {"name": "b", "parent": "a"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            state_dir = tmp / "must_not_exist" / "state"
            code = cli_main(
                [
                    "baseline",
                    "--skeleton",
                    str(bad_skeleton),
                    "--clips",
                    str(paths["clips"]),
                    "--state-machine",
                    str(paths["state_machine"]),
                    "--spec",
                    str(paths["chunk_spec"]),
                    "--state-dir",
                    str(state_dir),
                ]
            )
            self.assertEqual(code, 2)
            self.assertFalse(state_dir.exists())

    def test_undeclared_parameter_and_operation_codes(self) -> None:
        samples = build_samples()
        bad_sm = copy.deepcopy(samples["state_machine"])
        bad_sm["transitions"][0]["conditions"][0]["param"] = "not_declared"
        with self.assertRaises(Exception) as ctx:
            normalize_project(samples["skeleton"], samples["clips"], bad_sm, samples["chunk_spec"])
        self.assertEqual(ctx.exception.code, "E_PARAM_UNDECLARED")

        with self.assertRaises(Exception) as ctx:
            parse_edit_script({"edits": [{"op": "not_a_real_op"}]})
        self.assertEqual(ctx.exception.code, "E_OP_UNSUPPORTED")


class DeterminismAndIncrementalTests(unittest.TestCase, SampleMixin):
    def test_full_evaluation_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            project, _, _ = self.make_project(Path(td))
            trace_a, events_a = build_state_trace(project)
            pose_a, event_list_a = evaluate_full_pose(project, trace_a)
            pose_b, event_list_b = evaluate_full_pose(project)
            np.testing.assert_array_equal(pose_a, pose_b)
            self.assertEqual(events_a, event_list_a)
            self.assertEqual(event_list_a, event_list_b)
            required = {"frame", "source_state", "target_state", "decisive_condition"}
            self.assertTrue(event_list_a)
            for event in event_list_a:
                self.assertTrue(required.issubset(event))

    def test_concurrency_does_not_change_pose(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            project, _, _ = self.make_project(tmp)
            states = []
            for workers in (1, 3):
                state = tmp / f"state-w{workers}"
                engine = AnimEngine(project, state, max_workers=workers)
                report = engine.run_or_resume_full()
                self.assertEqual(report["status"], "completed")
                states.append(np.asarray(json.loads((state / "baseline_pose.json").read_text())["pose_matrices"]))
            np.testing.assert_array_equal(states[0], states[1])

    def test_incremental_closure_and_consistency(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            project, edits, _ = self.make_project(tmp)
            engine = AnimEngine(project, tmp / "state", max_workers=2)
            report = engine.run_edit_script(edits)
            self.assertEqual(report["status"], "completed", report.get("first_error"))
            self.assertIsNone(report["first_mismatch"])
            self.assertTrue(report["verification"]["events_consistent"])

            key_step = report["step_reports"][0]
            self.assertGreater(key_step["recomputed_chunks"], 0)
            self.assertLess(key_step["recomputed_chunks"], report["total_chunks"])
            self.assertIn("lower_arm_r", key_step["closure"]["affected_bones"])
            self.assertGreaterEqual(key_step["closure"]["affected_bones"].index("lower_arm_r"), 0)

            duration_step = report["step_reports"][2]
            self.assertGreater(duration_step["ported_chunks"], 0)
            self.assertLess(min(duration_step["dirty_chunks"]), report["total_chunks"] - 1)
            self.assertNotEqual(duration_step["dirty_chunks"], list(range(report["total_chunks"])))


class RecoveryTests(unittest.TestCase, SampleMixin):
    def test_graceful_interrupt_resume_and_digest_failure(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            project, _, _ = self.make_project(tmp)
            state = tmp / "state"
            interrupted = AnimEngine(
                project,
                state,
                max_workers=1,
                interrupt_after_chunks=2,
            ).run_or_resume_full()
            self.assertEqual(interrupted["status"], "interrupted")
            store = ChunkStore(state)
            self.assertTrue(store.marker_path.exists())
            self.assertEqual(len(list(store.chunks_dir.glob("chunk_*.json"))), 2)

            resumed = AnimEngine(project, state, max_workers=3).run_or_resume_full()
            self.assertEqual(resumed["status"], "completed")
            self.assertEqual(resumed["recovery_mode"], "same_version_partial_recompute")
            self.assertEqual(resumed["reused_chunks"], 2)
            self.assertEqual(resumed["recomputed_chunks"], resumed["total_chunks"] - 2)
            self.assertTrue(resumed["verification"]["consistent"])

            chunk_path = store.chunk_path(4)
            payload = json.loads(chunk_path.read_text(encoding="utf-8"))
            payload["pose"][0][0][0] += 0.5
            chunk_path.write_text(json.dumps(payload), encoding="utf-8")
            ok, reason, _ = store.verify_chunk(4, project_version(project))
            self.assertFalse(ok)
            self.assertEqual(reason, "content digest mismatch")

            recovered = AnimEngine(project, state, max_workers=2).run_or_resume_full()
            self.assertEqual(recovered["status"], "completed")
            self.assertEqual(recovered["recomputed_chunks"], 1)
            self.assertEqual(recovered["reused_chunks"], recovered["total_chunks"] - 1)
            self.assertTrue(recovered["verification"]["consistent"])

    def test_frame_count_mismatch_is_digest_failure(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            project, _, _ = self.make_project(tmp)
            state = tmp / "state"
            AnimEngine(project, state, max_workers=2).run_or_resume_full()
            store = ChunkStore(state)
            path = store.chunk_path(2)
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["frame_count"] += 1
            payload["digest"] = store.record_digest(payload)
            path.write_text(json.dumps(payload), encoding="utf-8")
            ok, reason, _ = store.verify_chunk(2, project_version(project), expected_frame_count=project["spec"]["chunk_frames"])
            self.assertFalse(ok)
            self.assertIn("frame count", reason)

            raw_payload = json.loads(path.read_text(encoding="utf-8"))
            raw_payload["frame_count"] += 1
            path.write_text(json.dumps(raw_payload), encoding="utf-8")
            ok, reason, _ = store.verify_chunk(2, project_version(project), expected_frame_count=project["spec"]["chunk_frames"])
            self.assertFalse(ok)
            self.assertEqual(reason, "content digest mismatch")

    def test_version_mismatch_archives_old_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            project, _, _ = self.make_project(tmp)
            state = tmp / "state"
            first = AnimEngine(project, state, max_workers=2).run_or_resume_full()
            self.assertTrue(first["ok"])

            edited = copy.deepcopy(project)
            edited["clips"][1]["duration_ms"] = 850
            report = AnimEngine(edited, state, max_workers=2).run_or_resume_full()
            self.assertEqual(report["recovery_mode"], "global_version_mismatch_full_recompute")
            self.assertEqual(report["recomputed_chunks"], report["total_chunks"])
            archive_files = list((state / "archive").rglob("chunk_*.json"))
            self.assertGreaterEqual(len(archive_files), report["total_chunks"])
            self.assertTrue(report["verification"]["consistent"])


class SchedulerTests(unittest.TestCase):
    def test_memory_budget_serializes_head_chunk(self) -> None:
        active = 0
        max_active = 0
        lock = threading.Lock()

        def task(index: int, stop_event: threading.Event):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.02)
            with lock:
                active -= 1
            return {"index": index}

        scheduler = BoundedChunkScheduler(
            max_workers=4,
            memory_budget_bytes=150,
            estimate_bytes=lambda _: 100,
        )
        outcome = scheduler.run([0, 1, 2], task)
        self.assertEqual(outcome.results.keys(), {0, 1, 2})
        self.assertGreater(outcome.memory_wait_events, 0)
        self.assertEqual(max_active, 1)

    def test_first_error_cancels_pending_and_keeps_success(self) -> None:
        def task(index: int, stop_event: threading.Event):
            if index == 1:
                raise RuntimeError("boom")
            time.sleep(0.01)
            return {"index": index}

        scheduler = BoundedChunkScheduler(
            max_workers=1,
            memory_budget_bytes=10_000,
            estimate_bytes=lambda _: 10,
        )
        outcome = scheduler.run([0, 1, 2, 3], task)
        self.assertIsNotNone(outcome.first_error)
        self.assertEqual(outcome.first_error["message"], "boom")
        self.assertIn(0, outcome.results)
        self.assertTrue(outcome.cancelled)


class SignalSubprocessTests(unittest.TestCase, SampleMixin):
    def test_terminate_signal_uses_chunk_boundary_and_recovers(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            self.make_project(tmp)
            state = tmp / "signal-state"
            cmd = [
                sys.executable,
                "-m",
                "animrecalc.cli",
                "baseline",
                "--sample-dir",
                str(tmp / "samples"),
                "--state-dir",
                str(state),
                "--workers",
                "1",
                "--chunk-delay-ms",
                "250",
            ]
            popen_kwargs: dict[str, object] = {"cwd": str(ROOT), "stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "text": True}
            if os.name == "nt":
                popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
            proc = subprocess.Popen(cmd, **popen_kwargs)  # type: ignore[arg-type]
            try:
                first_chunk = state / "chunks" / "chunk_000000.json"
                deadline = time.time() + 10
                while time.time() < deadline and not first_chunk.exists():
                    time.sleep(0.05)
                self.assertTrue(first_chunk.exists())
                time.sleep(0.1)
                if os.name == "nt":
                    proc.send_signal(signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
                else:
                    proc.send_signal(signal.SIGTERM)
                stdout, stderr = proc.communicate(timeout=15)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=5)
            self.assertEqual(proc.returncode, 75, msg=f"stdout={stdout}\nstderr={stderr}")
            self.assertTrue((state / "INTERRUPTED.json").exists())

            code = cli_main(
                [
                    "recover",
                    "--sample-dir",
                    str(tmp / "samples"),
                    "--state-dir",
                    str(state),
                    "--workers",
                    "2",
                ]
            )
            self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
