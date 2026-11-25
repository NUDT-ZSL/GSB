from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from diagnostic_engine.cache import build_frame_cache_key
from diagnostic_engine.checkpoint import CommitLock, CheckpointStore
from diagnostic_engine.engine import DiagnosticEngine, SimulatedTermination, arrays_byte_equal
from diagnostic_engine.errors import DiagnosticError, ErrorCode
from diagnostic_engine.invalidation import plan_change, validate_recompute_reuse_partition
from diagnostic_engine.models import ClipTask, validate_manifest_records, validate_noise_array
from diagnostic_engine.propagation import generate_continuous
from diagnostic_engine.resources import ResourceLimits, plan_resources
from diagnostic_engine.sample import generate_sample


def make_task(**overrides) -> ClipTask:
    data = {
        "clip_id": "clip_test",
        "total_frames": 12,
        "frame_shape": [8, 8, 3],
        "noise_seed": 42,
        "motion_mode": "translate",
        "block_size": 4,
        "propagation_steps": 2,
        "revision": 1,
    }
    data.update(overrides)
    return ClipTask.from_dict(data)


class DeterminismTests(unittest.TestCase):
    def test_chunked_matches_continuous_for_all_motion_modes(self) -> None:
        for mode in ("translate", "ripple", "diffuse"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                task = make_task(
                    clip_id=f"clip_{mode}",
                    motion_mode=mode,
                    frame_shape=[6, 7] if mode == "diffuse" else [8, 8, 1],
                    block_size=3,
                    total_frames=10,
                )
                engine = DiagnosticEngine(Path(tmp) / "state")
                report = engine.run_clip(task)
                chunked = engine.load_complete_frame_array(task)
                continuous = generate_continuous(task)
                self.assertTrue(arrays_byte_equal(chunked, continuous))
                self.assertEqual(chunked.tobytes(), continuous.tobytes())
                self.assertTrue(report["complete"])

    def test_different_block_sizes_produce_same_bytes(self) -> None:
        baseline_task = make_task(block_size=12)
        chunked_task = make_task(block_size=3)
        with tempfile.TemporaryDirectory() as tmp:
            baseline_engine = DiagnosticEngine(Path(tmp) / "baseline")
            chunk_engine = DiagnosticEngine(Path(tmp) / "chunk")
            baseline_engine.run_clip(baseline_task)
            chunk_engine.run_clip(chunked_task)
            baseline = baseline_engine.load_complete_frame_array(baseline_task)
            chunked = chunk_engine.load_complete_frame_array(chunked_task)
            self.assertEqual(chunked.tobytes(), baseline.tobytes())


class ValidationTests(unittest.TestCase):
    def test_invalid_block_size_and_steps_have_structured_fields(self) -> None:
        for field_name, value in (("block_size", 0), ("propagation_steps", -2)):
            with self.subTest(field=field_name):
                with self.assertRaises(DiagnosticError) as context:
                    make_task(**{field_name: value})
                error = context.exception
                self.assertEqual(error.code, ErrorCode.PROPAGATION_INVALID)
                self.assertEqual(error.clip_id, "clip_test")
                self.assertEqual(error.field, field_name)

    def test_duplicate_clip_id(self) -> None:
        record = make_task().to_dict()
        result = validate_manifest_records([record, dict(record)])
        self.assertFalse(result.ok)
        self.assertEqual(result.errors[0]["code"], ErrorCode.DUPLICATE_CLIP_ID)
        self.assertEqual(result.errors[0]["field"], "clip_id")
        self.assertEqual(result.errors[0]["clip_id"], "clip_test")

    def test_noise_array_dimension_mismatch(self) -> None:
        task = make_task(frame_shape=[4, 4, 1], total_frames=5)
        noise = np.zeros((4, 4, 4, 1), dtype=np.uint8)
        with self.assertRaises(DiagnosticError) as context:
            validate_noise_array(task, noise)
        self.assertEqual(context.exception.code, ErrorCode.NOISE_SHAPE_MISMATCH)
        self.assertEqual(context.exception.field, "noise")

    def test_revision_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine = DiagnosticEngine(Path(tmp))
            engine.run_clip(make_task(revision=2))
            with self.assertRaises(DiagnosticError) as context:
                engine.run_clip(make_task(revision=1))
            self.assertEqual(context.exception.code, ErrorCode.REVISION_ROLLBACK)
            self.assertEqual(context.exception.field, "revision")


class RecoveryTests(unittest.TestCase):
    def test_simulated_kill_discards_partial_block_and_recovers_bytes(self) -> None:
        task = make_task()
        with tempfile.TemporaryDirectory() as tmp:
            engine = DiagnosticEngine(Path(tmp))
            with self.assertRaises(SimulatedTermination):
                engine.run_clip(task, terminate_after_frame=5)

            checkpoint = engine.checkpoints.load(task.clip_id)
            self.assertEqual(checkpoint["version"], 1)
            self.assertEqual(checkpoint["next_frame"], 4)

            recovered_engine = DiagnosticEngine(Path(tmp))
            report = recovered_engine.run_clip(task)
            recovered = recovered_engine.load_complete_frame_array(task)
            baseline = generate_continuous(task)

            self.assertEqual(recovered.tobytes(), baseline.tobytes())
            self.assertEqual(report["reused_frames"], [0, 1, 2, 3])
            self.assertEqual(report["computed_frames"], [4, 5, 6, 7, 8, 9, 10, 11])
            self.assertEqual(report["noise_generated_frames"], report["computed_frames"])
            self.assertTrue(report["recovery"]["recovered"])
            self.assertEqual(report["recovery"]["checkpoint_version_before"], 1)
            self.assertGreaterEqual(
                report["recovery"]["discarded_uncommitted_frame_count"], 1
            )

    def test_actual_sigkill_subprocess_recovers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sample_info = generate_sample(root / "samples")
            state = root / "state"
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
            command_prefix = [
                sys.executable,
                "-m",
                "diagnostic_engine.cli",
                "run",
                "--manifest",
                sample_info["manifest"],
                "--state-dir",
                str(state),
                "--clip-id",
                "clip_alpha",
            ]
            killed = subprocess.run(
                command_prefix
                + ["--terminate-after-frame", "5", "--sigkill"],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(killed.returncode, 0)

            checkpoint = json.loads((state / "checkpoints" / "clip_alpha.json").read_text())
            self.assertEqual(checkpoint["next_frame"], 4)

            resumed = subprocess.run(
                command_prefix + ["--report", str(root / "report.json")],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(resumed.returncode, 0, resumed.stderr + resumed.stdout)
            report = json.loads((root / "report.json").read_text())
            self.assertTrue(report["ok"])

            engine = DiagnosticEngine(state)
            task = next(
                task
                for task in generate_sample_tasks_from_records()
                if task.clip_id == "clip_alpha"
            )
            recovered = engine.load_complete_frame_array(task)
            self.assertEqual(recovered.tobytes(), generate_continuous(task).tobytes())


def generate_sample_tasks_from_records():
    from diagnostic_engine.sample import sample_tasks

    return sample_tasks()


class CheckpointConcurrencyTests(unittest.TestCase):
    def test_version_conflict_rejects_stale_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = CheckpointStore(Path(tmp))
            store.commit("c", {"clip_id": "c", "version": 1}, 0)
            with self.assertRaises(DiagnosticError) as context:
                store.commit("c", {"clip_id": "c", "version": 1}, 0)
            self.assertEqual(context.exception.code, ErrorCode.CHECKPOINT_CONFLICT)
            self.assertEqual(context.exception.details["actual_version"], 1)

    def test_nonblocking_stage_busy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock = CommitLock(Path(tmp))
            lock.acquire_nonblocking()
            second = CommitLock(Path(tmp))
            with self.assertRaises(DiagnosticError) as context:
                second.acquire_nonblocking()
            self.assertEqual(context.exception.code, ErrorCode.STAGE_BUSY)
            lock.release()

    def test_commit_while_global_lock_held_is_busy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = CheckpointStore(Path(tmp))
            with store.lock:
                with self.assertRaises(DiagnosticError) as context:
                    store.commit("clip", {"clip_id": "clip", "version": 1}, 0)
            self.assertEqual(context.exception.code, ErrorCode.STAGE_BUSY)

    def test_concurrent_thread_commits_one_outcome_is_busy_or_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = CheckpointStore(Path(tmp))
            barrier = threading.Barrier(2)
            outcomes: list[str] = []

            def worker(index: int) -> None:
                barrier.wait()
                try:
                    store.commit(
                        "clip",
                        {"clip_id": "clip", "version": 1, "worker": index},
                        0,
                    )
                    outcomes.append("committed")
                except DiagnosticError as error:
                    outcomes.append(error.code)

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(outcomes.count("committed"), 1)
            self.assertEqual(len(outcomes), 2)
            self.assertTrue(
                ErrorCode.STAGE_BUSY in outcomes
                or ErrorCode.CHECKPOINT_CONFLICT in outcomes
            )


class CacheAndInvalidationTests(unittest.TestCase):
    def valid_material(self, **overrides):
        material = {
            "clip_id": "c",
            "noise_seed": 1,
            "frame_range": [3, 4],
            "frame_index": 3,
            "block_size": 4,
            "propagation_steps": 2,
            "upstream_hash": "a" * 64,
        }
        material.update(overrides)
        return material

    def test_missing_upstream_hash_is_invalid(self) -> None:
        material = self.valid_material(upstream_hash=None)
        with self.assertRaises(DiagnosticError) as context:
            build_frame_cache_key(material)
        self.assertEqual(context.exception.code, ErrorCode.CACHE_KEY_INVALID)
        self.assertIn("upstream_hash", context.exception.field)

    def test_key_changes_with_required_dependencies(self) -> None:
        base = build_frame_cache_key(self.valid_material())
        variants = [
            {"noise_seed": 2},
            {"clip_id": "other"},
            {"frame_range": [4, 5], "frame_index": 4},
            {"block_size": 8},
            {"propagation_steps": 3},
            {"upstream_hash": "b" * 64},
        ]
        for variant in variants:
            with self.subTest(variant=variant):
                self.assertNotEqual(base, build_frame_cache_key(self.valid_material(**variant)))

    def test_partial_invalidation_reuses_prefix(self) -> None:
        old = make_task(revision=1)
        new = make_task(revision=2, change_frame=5)
        plan = plan_change(old, new)
        self.assertEqual(plan.affected_frames, tuple(range(4, 12)))
        self.assertEqual(plan.reused_frames, tuple(range(0, 4)))

        with tempfile.TemporaryDirectory() as tmp:
            engine = DiagnosticEngine(Path(tmp))
            engine.run_clip(old)
            report = engine.run_clip(new, change_plan=plan, old_task=old)
            self.assertEqual(report["recomputed_frames"], list(range(4, 12)))
            self.assertEqual(report["reused_frames"], list(range(0, 4)))
            self.assertEqual(report["noise_generated_frames"], list(range(4, 12)))
            self.assertTrue(engine.self_check("clip_test")["ok"])

    def test_global_seed_change_recomputes_everything(self) -> None:
        old = make_task(revision=1, noise_seed=1)
        new = make_task(revision=2, noise_seed=2)
        plan = plan_change(old, new)
        self.assertEqual(plan.affected_start_frame, 0)
        self.assertEqual(plan.reused_frames, ())
        with tempfile.TemporaryDirectory() as tmp:
            engine = DiagnosticEngine(Path(tmp))
            engine.run_clip(old)
            report = engine.run_clip(new)
            self.assertEqual(report["reused_frames"], [])
            self.assertEqual(report["recomputed_frames"], list(range(12)))

    def test_targeted_seed_and_steps_changes_reuse_prefix(self) -> None:
        change_cases = (
            ("noise_seed", 99),
            ("propagation_steps", 4),
        )
        for field_name, new_value in change_cases:
            with self.subTest(field=field_name), tempfile.TemporaryDirectory() as tmp:
                old = make_task(revision=1)
                new_data = old.to_dict()
                new_data[field_name] = new_value
                new_data["revision"] = 2
                new_data["change_frame"] = 6
                new = ClipTask.from_dict(new_data)
                plan = plan_change(old, new)
                engine = DiagnosticEngine(Path(tmp))
                engine.run_clip(old)
                report = engine.run_clip(new, change_plan=plan, old_task=old)
                self.assertEqual(report["reused_frames"], list(range(0, 4)))
                self.assertEqual(report["recomputed_frames"], list(range(4, 12)))
                self.assertTrue(engine.self_check(old.clip_id)["ok"])

    def test_partition_mismatch_code(self) -> None:
        with self.assertRaises(DiagnosticError) as context:
            validate_recompute_reuse_partition("c", 4, [0, 1], [1, 2])
        self.assertEqual(context.exception.code, ErrorCode.INVALIDATION_MISMATCH)
        self.assertIn(3, context.exception.details["missing"])
        self.assertIn(1, context.exception.details["overlap"])


class SelfCheckAndResourceTests(unittest.TestCase):
    def test_corrupt_checkpoint_hash_reports_first_frame_and_version(self) -> None:
        task = make_task()
        with tempfile.TemporaryDirectory() as tmp:
            engine = DiagnosticEngine(Path(tmp))
            engine.run_clip(task)
            path = engine.checkpoints.path_for(task.clip_id)
            payload = json.loads(path.read_text())
            payload["frame_hashes"]["0"] = "0" * 64
            path.write_text(json.dumps(payload))

            with self.assertRaises(DiagnosticError) as context:
                engine.self_check(task.clip_id)
            error = context.exception
            self.assertEqual(error.code, ErrorCode.STATE_INCONSISTENT)
            self.assertEqual(error.details["first_deviation_frame"], 0)
            self.assertEqual(error.details["checkpoint_version"], 3)
            self.assertIsNotNone(engine.latest_error())

    def test_block_count_limit(self) -> None:
        task = make_task(total_frames=12, block_size=4)
        with self.assertRaises(DiagnosticError) as context:
            plan_resources(task, ResourceLimits(max_blocks_per_clip=2))
        self.assertEqual(context.exception.code, ErrorCode.RESOURCE_LIMIT)
        self.assertEqual(context.exception.details["category"], "block_count")

    def test_memory_limit_degrades_block_size(self) -> None:
        task = make_task(frame_shape=[8, 8, 3], block_size=4)
        limits = ResourceLimits(max_resident_bytes=800)
        plan = plan_resources(task, limits)
        self.assertEqual(plan.effective_block_size, 1)
        self.assertIn("resident_memory", plan.degradations)
        with tempfile.TemporaryDirectory() as tmp:
            report = DiagnosticEngine(Path(tmp), limits=limits).run_clip(task)
            self.assertEqual(report["resource_plan"]["effective_block_size"], 1)
            self.assertTrue(report["degradation"]["degraded"])

    def test_propagation_state_limit_degrades(self) -> None:
        task = make_task(block_size=4, propagation_steps=10)
        plan = plan_resources(
            task,
            ResourceLimits(max_propagation_states=15, max_resident_bytes=1024 * 1024),
        )
        self.assertEqual(plan.effective_block_size, 1)
        self.assertIn("propagation_states", plan.degradations)

    def test_propagation_steps_over_hard_limit(self) -> None:
        task = make_task(propagation_steps=10)
        with self.assertRaises(DiagnosticError) as context:
            plan_resources(task, ResourceLimits(max_propagation_states=5))
        self.assertEqual(context.exception.code, ErrorCode.RESOURCE_LIMIT)


if __name__ == "__main__":
    unittest.main()
