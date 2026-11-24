from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from narrative_workbench.engine import SimulationKernel
from narrative_workbench.errors import (
    BudgetError,
    DigestError,
    InputError,
    Interrupted,
    ReplayError,
    RuntimeTaskError,
)
from narrative_workbench.persistence import SnapshotStore
from narrative_workbench.replay import ensure_replay_ok, verify_replay
from narrative_workbench.runner import RunnerConfig, WorkbenchRunner
from narrative_workbench.scheduler import BoundedTaskPool, MemoryBudget
from narrative_workbench.validation import load_bundle

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"


def sample_paths() -> dict[str, str]:
    return {
        "state_path": str(SAMPLES / "state.json"),
        "story_path": str(SAMPLES / "story.json"),
        "storyboard_path": str(SAMPLES / "storyboard.json"),
        "run_path": str(SAMPLES / "run.json"),
    }


def run_machine():
    bundle = load_bundle(**sample_paths())
    machine = SimulationKernel(bundle)
    while not machine.complete:
        machine.step()
    return bundle, machine


def run_runner(
    tmp_root: Path,
    *,
    name: str,
    workers: int = 2,
    memory_budget_bytes: int = 64 * 1024 * 1024,
    frame_block_size: int = 4,
    fault_after_boundary: int | None = None,
    fault_signal: str = "sigint",
):
    bundle = load_bundle(**sample_paths())
    runner = WorkbenchRunner(
        bundle,
        state_dir=tmp_root / f"{name}-state",
        output_dir=tmp_root / f"{name}-output",
        config=RunnerConfig(
            workers=workers,
            memory_budget_bytes=memory_budget_bytes,
            frame_block_size=frame_block_size,
            fault_after_boundary=fault_after_boundary,
            fault_signal=fault_signal,
        ),
    )
    return bundle, runner


def frame_pixels(bundle, state_dir: Path) -> dict[int, str]:
    store = SnapshotStore(
        state_dir,
        version_id=bundle.version_id,
        run_id=bundle.run_id,
        canvas_width=bundle.storyboard.width,
        canvas_height=bundle.storyboard.height,
    )
    recovery = store.recover()
    frames = []
    for payload in recovery["frame_blocks"].values():
        frames.extend(payload["frames"])
    return {
        int(frame["frame_index"]): frame["pixels_base64"] for frame in sorted(frames, key=lambda item: item["frame_index"])
    }


class ValidationTests(unittest.TestCase):
    def test_sample_validates(self) -> None:
        bundle = load_bundle(**sample_paths())
        self.assertEqual(bundle.state.fixed_scale, 1_000_000)
        self.assertEqual(len(bundle.story.scenes), 3)

    def test_duplicate_scene_and_bad_refs(self) -> None:
        state = json.loads((SAMPLES / "state.json").read_text())
        story = json.loads((SAMPLES / "story.json").read_text())
        storyboard = json.loads((SAMPLES / "storyboard.json").read_text())
        story["scenes"].append(story["scenes"][0])
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            story_path = tmp_path / "story.json"
            story_path.write_text(json.dumps(story))
            with self.assertRaises(InputError) as caught:
                load_bundle(
                    state_path=SAMPLES / "state.json",
                    story_path=story_path,
                    storyboard_path=SAMPLES / "storyboard.json",
                    run_path=SAMPLES / "run.json",
                )
            self.assertEqual(caught.exception.code, "E_SCENE_DUP")

    def test_undeclared_key_and_threshold_domain(self) -> None:
        story = json.loads((SAMPLES / "story.json").read_text())
        story["scenes"][0]["events"][0]["effects"][0]["key"] = "item.missing"
        with tempfile.TemporaryDirectory() as tmp:
            story_path = Path(tmp) / "story.json"
            story_path.write_text(json.dumps(story))
            with self.assertRaises(InputError) as caught:
                load_bundle(
                    state_path=SAMPLES / "state.json",
                    story_path=story_path,
                    storyboard_path=SAMPLES / "storyboard.json",
                    run_path=SAMPLES / "run.json",
                )
            self.assertEqual(caught.exception.code, "E_STATE_KEY")

        story = json.loads((SAMPLES / "story.json").read_text())
        story["scenes"][0]["events"][3]["preconditions"][0]["value"] = "100"
        with tempfile.TemporaryDirectory() as tmp:
            story_path = Path(tmp) / "story.json"
            story_path.write_text(json.dumps(story))
            with self.assertRaises(InputError) as caught:
                load_bundle(
                    state_path=SAMPLES / "state.json",
                    story_path=story_path,
                    storyboard_path=SAMPLES / "storyboard.json",
                    run_path=SAMPLES / "run.json",
                )
            self.assertEqual(caught.exception.code, "E_DOMAIN")

    def test_storyboard_rect_color_and_time(self) -> None:
        storyboard = json.loads((SAMPLES / "storyboard.json").read_text())
        storyboard["scenes"]["s1"]["template"][0]["rect"] = [180, 0, 50, 20]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "storyboard.json"
            path.write_text(json.dumps(storyboard))
            with self.assertRaises(InputError) as caught:
                load_bundle(
                    state_path=SAMPLES / "state.json",
                    story_path=SAMPLES / "story.json",
                    storyboard_path=path,
                    run_path=SAMPLES / "run.json",
                )
            self.assertEqual(caught.exception.code, "E_RECT")

        storyboard = json.loads((SAMPLES / "storyboard.json").read_text())
        storyboard["scenes"]["s1"]["template"][0]["color"] = "red"
        del storyboard["scenes"]["s1"]["template"][0]["gray"]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "storyboard.json"
            path.write_text(json.dumps(storyboard))
            with self.assertRaises(InputError) as caught:
                load_bundle(
                    state_path=SAMPLES / "state.json",
                    story_path=SAMPLES / "story.json",
                    storyboard_path=path,
                    run_path=SAMPLES / "run.json",
                )
            self.assertEqual(caught.exception.code, "E_COLOR")

        story = json.loads((SAMPLES / "story.json").read_text())
        story["scenes"][0]["start_time"] = -1
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "story.json"
            path.write_text(json.dumps(story))
            with self.assertRaises(InputError) as caught:
                load_bundle(
                    state_path=SAMPLES / "state.json",
                    story_path=path,
                    storyboard_path=SAMPLES / "storyboard.json",
                    run_path=SAMPLES / "run.json",
                )
            self.assertEqual(caught.exception.code, "E_TIME")

    def test_invalid_cli_input_creates_no_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bad_story = json.loads((SAMPLES / "story.json").read_text())
            bad_story["scenes"][0]["events"][0]["effects"][0]["key"] = "item.missing"
            story_path = root / "bad-story.json"
            story_path.write_text(json.dumps(bad_story))
            state_dir = root / "state"
            output_dir = root / "output"
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "narrative_workbench",
                    "run",
                    "--state",
                    str(SAMPLES / "state.json"),
                    "--story",
                    str(story_path),
                    "--storyboard",
                    str(SAMPLES / "storyboard.json"),
                    "--run",
                    str(SAMPLES / "run.json"),
                    "--state-dir",
                    str(state_dir),
                    "--output-dir",
                    str(output_dir),
                    "--quiet",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("E_STATE_KEY", result.stderr)
            self.assertFalse(state_dir.exists())
            self.assertFalse(output_dir.exists())


class EngineTests(unittest.TestCase):
    def test_fixed_is_authoritative_and_float_diagnostic(self) -> None:
        bundle, machine = run_machine()
        self.assertEqual(
            machine.summary()["event_sequence"],
            [
                "s1.e_open",
                "s1.e_mira_a",
                "s1.e_mira_b",
                "s1.e_gate",
                "s1.e_high_after_gate",
                "s1.e_collect",
                "s2.e_evidence",
                "s2.e_jon",
                "s3.e_signal",
                "s3.e_final",
            ],
        )
        self.assertEqual(machine.fixed["rel.mira"], 1_050_000)
        first = machine.payload["first_exceeded"]
        self.assertIsNotNone(first)
        self.assertEqual(first["event_id"], "s1.e_mira_b")
        self.assertEqual(first["state_key"], "rel.mira")
        self.assertGreater(float(first["absolute_error"]), float(first["tolerance"]))

        gate = next(
            record for record in machine.ledger if record.get("event_id") == "s1.e_gate"
        )
        result = gate["preconditions"][0]
        self.assertTrue(result["fixed_passed"])
        self.assertFalse(result["reference_passed"])
        self.assertTrue(any(
            item["event_id"] == "s1.e_gate"
            and item["state_key"] == "rel.mira"
            for item in machine.payload["reference_mismatches"]
        ))

    def test_ledger_contains_causal_records(self) -> None:
        _bundle, machine = run_machine()
        for index, record in enumerate(machine.ledger):
            self.assertEqual(record["record_id"], index)
            self.assertEqual(record["frame_index"], index)
            if index:
                self.assertEqual(record["causal_parent_record_id"], index - 1)
        branch = next(
            record for record in machine.ledger if record["kind"] == "branch"
        )
        self.assertIn("scene_sequence", branch)
        self.assertIn("upstream_choices", branch)
        self.assertEqual(branch["scene_id"], "s1")


class DeterminismAndReplayTests(unittest.TestCase):
    def test_worker_counts_produce_identical_results(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle1, runner1 = run_runner(root, name="w1", workers=1)
            result1 = runner1.run()
            bundle4, runner4 = run_runner(root, name="w4", workers=4)
            result4 = runner4.run()
            self.assertEqual(result1, result4)
            ledger1 = json.loads(
                (root / "w1-output" / "runs" / bundle1.version_id / bundle1.run_id / "ledger.json").read_text()
            )
            ledger4 = json.loads(
                (root / "w4-output" / "runs" / bundle4.version_id / bundle4.run_id / "ledger.json").read_text()
            )
            self.assertEqual(ledger1, ledger4)
            self.assertEqual(
                frame_pixels(bundle1, root / "w1-state"),
                frame_pixels(bundle4, root / "w4-state"),
            )

    def test_replay_report_trace_and_manual_mismatch(self) -> None:
        bundle, machine = run_machine()
        from narrative_workbench.persistence import SnapshotStore

        with tempfile.TemporaryDirectory() as tmp:
            store = SnapshotStore(
                tmp,
                version_id=bundle.version_id,
                run_id=bundle.run_id,
                canvas_width=bundle.storyboard.width,
                canvas_height=bundle.storyboard.height,
            )
            store.prepare()
            # Generate the deterministic frame plan directly, without relying only on runner output.
            from narrative_workbench.storyboard import (
                build_frame_specs,
                frame_to_payload,
                plan_frame_blocks,
                render_frame,
            )

            specs = build_frame_specs(bundle, machine.ledger)
            descriptors = plan_frame_blocks(bundle, specs, 4)
            frames = []
            for descriptor in descriptors:
                for spec in descriptor["frames"]:
                    frames.append(frame_to_payload(spec, render_frame(bundle, spec)))
            frames.sort(key=lambda item: item["frame_index"])
            report = verify_replay(bundle, machine.ledger, frames)
            self.assertTrue(report["ok"])
            self.assertEqual(report["checked_frame_count"], len(machine.ledger))
            self.assertEqual(report["trace"][0]["record_id"], 0)
            self.assertEqual(report["trace"][-1]["scene_id"], "s3")

            frames[4]["visible_markers"][0]["fixed_value"] += 1
            bad_report = verify_replay(bundle, machine.ledger, frames)
            self.assertFalse(bad_report["ok"])
            self.assertEqual(bad_report["code"], "E_REPLAY")
            self.assertEqual(bad_report["first_mismatch"]["frame_index"], 4)
            self.assertEqual(bad_report["first_mismatch"]["scene_id"], "s1")
            with self.assertRaises(ReplayError):
                ensure_replay_ok(bad_report)


class SnapshotRecoveryTests(unittest.TestCase):
    def test_tampered_sim_snapshot_falls_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle, runner = run_runner(root, name="case")
            runner.run()
            store = SnapshotStore(
                root / "case-state",
                version_id=bundle.version_id,
                run_id=bundle.run_id,
                canvas_width=bundle.storyboard.width,
                canvas_height=bundle.storyboard.height,
            )
            manifest = json.loads((store.sim_dir / "manifest.json").read_text())
            latest = max(manifest["entries"], key=lambda item: item["sequence"])
            path = store.sim_dir / latest["file"]
            envelope = json.loads(path.read_text())
            envelope["payload"]["ledger"][0]["scene_id"] = "tampered"
            path.write_text(json.dumps(envelope))
            recovery = store.recover()
            self.assertTrue(recovery["report"]["errors"])
            self.assertTrue(
                all(error["code"] == "E_DIGEST" for error in recovery["report"]["errors"])
            )
            self.assertEqual(
                recovery["report"]["sim"]["used_sequence"],
                latest["sequence"] - 1,
            )
            self.assertIsNotNone(recovery["sim_payload"])

    def test_tampered_frame_is_reported_and_regenerated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle, runner = run_runner(root, name="frame")
            first = runner.run()
            store = SnapshotStore(
                root / "frame-state",
                version_id=bundle.version_id,
                run_id=bundle.run_id,
                canvas_width=bundle.storyboard.width,
                canvas_height=bundle.storyboard.height,
            )
            manifest = json.loads((store.frames_dir / "manifest.json").read_text())
            frame_entry = manifest["entries"][0]
            frame_path = store.frames_dir / frame_entry["file"]
            envelope = json.loads(frame_path.read_text())
            envelope["payload"]["frames"][0]["pixels_base64"] = "AAAA"
            frame_path.write_text(json.dumps(envelope))

            _bundle2, resumed = run_runner(root, name="frame")
            result = resumed.run()
            self.assertTrue(result["complete"])
            self.assertGreater(result["recovery_error_count"], 0)
            recovery = json.loads(
                (
                    root
                    / "frame-output"
                    / "runs"
                    / bundle.version_id
                    / bundle.run_id
                    / "recovery_report.json"
                ).read_text()
            )
            self.assertTrue(any(error["code"] == "E_DIGEST" for error in recovery["errors"]))
            self.assertEqual(result["frame_count"], first["frame_count"])

    def test_version_change_archives_old_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle, runner = run_runner(root, name="ver")
            runner.run()
            story = json.loads((SAMPLES / "story.json").read_text())
            story["scenes"][0]["title"] = "Platform Changed"
            changed_story = root / "changed-story.json"
            changed_story.write_text(json.dumps(story))
            changed_bundle = load_bundle(
                state_path=SAMPLES / "state.json",
                story_path=changed_story,
                storyboard_path=SAMPLES / "storyboard.json",
                run_path=SAMPLES / "run.json",
            )
            changed_runner = WorkbenchRunner(
                changed_bundle,
                state_dir=root / "ver-state",
                output_dir=root / "ver-output",
                config=RunnerConfig(workers=2),
            )
            result = changed_runner.run()
            self.assertTrue(result["complete"])
            archive = root / "ver-state" / "archive"
            self.assertTrue(archive.exists())
            self.assertTrue(any(archive.iterdir()))
            self.assertFalse(
                (root / "ver-state" / "snapshots" / bundle.version_id).exists()
            )


class InterruptRecoveryTests(unittest.TestCase):
    def test_sigint_boundary_and_resume_match_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base_bundle, base_runner = run_runner(root, name="base")
            baseline = base_runner.run()
            baseline_pixels = frame_pixels(base_bundle, root / "base-state")

            interrupted_bundle, interrupted_runner = run_runner(
                root,
                name="sigint",
                fault_after_boundary=5,
                fault_signal="sigint",
            )
            with self.assertRaises(Interrupted) as caught:
                interrupted_runner.run()
            self.assertEqual(caught.exception.exit_code, 130)
            marker_path = root / "sigint-state" / "interrupt.json"
            self.assertTrue(marker_path.exists())
            marker = json.loads(marker_path.read_text())
            self.assertEqual(marker["code"], "E_INTERRUPTED")
            result_path = (
                root
                / "sigint-output"
                / "runs"
                / interrupted_bundle.version_id
                / interrupted_bundle.run_id
                / "result.json"
            )
            self.assertFalse(result_path.exists())

            _bundle, resumed = run_runner(root, name="sigint")
            resumed_result = resumed.run()
            self.assertEqual(resumed_result, baseline)
            self.assertEqual(
                frame_pixels(interrupted_bundle, root / "sigint-state"),
                baseline_pixels,
            )
            self.assertFalse(marker_path.exists())

    def test_sigkill_resume_matches_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base_bundle, base_runner = run_runner(root, name="kbase")
            baseline = base_runner.run()
            baseline_pixels = frame_pixels(base_bundle, root / "kbase-state")
            common = [
                sys.executable,
                "-m",
                "narrative_workbench",
                "run",
                "--state",
                str(SAMPLES / "state.json"),
                "--story",
                str(SAMPLES / "story.json"),
                "--storyboard",
                str(SAMPLES / "storyboard.json"),
                "--run",
                str(SAMPLES / "run.json"),
                "--state-dir",
                str(root / "kill-state"),
                "--output-dir",
                str(root / "kill-output"),
                "--workers",
                "2",
                "--quiet",
            ]
            killed = subprocess.run(
                common + ["--fault-after-boundary", "7", "--fault-signal", "sigkill"],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(killed.returncode, 0)
            self.assertFalse((root / "kill-state" / "interrupt.json").exists())
            result_path = (
                root
                / "kill-output"
                / "runs"
                / baseline["version_id"]
                / baseline["run_id"]
                / "result.json"
            )
            self.assertFalse(result_path.exists())
            resumed = subprocess.run(
                common,
                cwd=ROOT,
                capture_output=True,
                text=True,
            )
            self.assertEqual(resumed.returncode, 0, resumed.stderr)
            resumed_result = json.loads(resumed.stdout)
            self.assertEqual(resumed_result, baseline)
            self.assertEqual(
                frame_pixels(base_bundle, root / "kill-state"),
                baseline_pixels,
            )

    def test_sigint_at_frame_block_and_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base_bundle, base_runner = run_runner(root, name="fbase")
            baseline = base_runner.run()
            baseline_pixels = frame_pixels(base_bundle, root / "fbase-state")

            _bundle, interrupted = run_runner(
                root,
                name="frame-int",
                workers=2,
                fault_after_boundary=18,
                fault_signal="sigint",
            )
            with self.assertRaises(Interrupted) as caught:
                interrupted.run()
            self.assertEqual(caught.exception.boundary, "frame_block")
            marker = json.loads(
                (root / "frame-int-state" / "interrupt.json").read_text()
            )
            self.assertEqual(marker["boundary"], "frame_block")
            self.assertGreaterEqual(
                marker["snapshot_sequence"],
                1,
            )
            self.assertFalse(
                list(
                    (root / "frame-int-output").glob(
                        "runs/*/*/result.json"
                    )
                )
            )

            _bundle2, resumed = run_runner(root, name="frame-int", workers=4)
            resumed_result = resumed.run()
            self.assertEqual(resumed_result, baseline)
            self.assertEqual(
                frame_pixels(base_bundle, root / "frame-int-state"),
                baseline_pixels,
            )

    def test_sigkill_at_frame_block_and_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base_bundle, base_runner = run_runner(root, name="fkbase")
            baseline = base_runner.run()
            baseline_pixels = frame_pixels(base_bundle, root / "fkbase-state")
            common = [
                sys.executable,
                "-m",
                "narrative_workbench",
                "run",
                "--state",
                str(SAMPLES / "state.json"),
                "--story",
                str(SAMPLES / "story.json"),
                "--storyboard",
                str(SAMPLES / "storyboard.json"),
                "--run",
                str(SAMPLES / "run.json"),
                "--state-dir",
                str(root / "fk-state"),
                "--output-dir",
                str(root / "fk-output"),
                "--workers",
                "3",
                "--quiet",
            ]
            killed = subprocess.run(
                common + ["--fault-after-boundary", "19", "--fault-signal", "sigkill"],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(killed.returncode, 0)
            self.assertFalse((root / "fk-state" / "interrupt.json").exists())
            self.assertFalse(
                list((root / "fk-output").glob("runs/*/*/result.json"))
            )
            resumed = subprocess.run(common + ["--workers", "4"], cwd=ROOT, capture_output=True, text=True)
            # argparse keeps the last --workers value; this deliberately resumes with 4.
            self.assertEqual(resumed.returncode, 0, resumed.stderr)
            self.assertEqual(json.loads(resumed.stdout), baseline)
            self.assertEqual(
                frame_pixels(base_bundle, root / "fk-state"),
                baseline_pixels,
            )


class ConcurrencyAndBudgetTests(unittest.TestCase):
    def test_budget_below_one_frame_block_is_rejected_without_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _bundle, runner = run_runner(
                root, name="budget", memory_budget_bytes=64 * 1024
            )
            with self.assertRaises(BudgetError):
                runner.run()
            self.assertFalse((root / "budget-state").exists())
            self.assertFalse((root / "budget-output").exists())

    def test_exact_single_block_budget_and_memory_waiting(self) -> None:
        block_bytes = 192 * 108 * 4 * 2 + 32768
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _bundle, runner = run_runner(
                root,
                name="tight",
                workers=4,
                memory_budget_bytes=block_bytes + 128 * 1024,
            )
            result = runner.run()
            self.assertTrue(result["complete"])
            self.assertTrue(result["replay_ok"])

        budget = MemoryBudget(10)
        self.assertTrue(budget.try_reserve(10))
        self.assertFalse(budget.try_reserve(1))
        released = threading.Event()

        def release_later() -> None:
            time.sleep(0.05)
            budget.release(10)
            released.set()

        thread = threading.Thread(target=release_later)
        thread.start()
        self.assertTrue(budget.reserve(10, stop=threading.Event()))
        self.assertTrue(released.is_set())
        budget.release(10)
        self.assertEqual(budget.max_seen_bytes, 10)

    def test_task_failure_cancels_and_returns_first_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _bundle, runner = run_runner(root, name="failure", workers=2)

            def exploding_render(bundle, descriptor):
                if descriptor["block_index"] == 1:
                    time.sleep(0.02)
                    raise RuntimeError("synthetic frame failure")
                time.sleep(0.08)
                return {
                    "block_index": descriptor["block_index"],
                    "scene_id": descriptor["scene_id"],
                    "start_frame": descriptor["start_frame"],
                    "end_frame": descriptor["end_frame"],
                    "frames": [],
                }

            with mock.patch(
                "narrative_workbench.runner.render_frame_block",
                side_effect=exploding_render,
            ):
                with self.assertRaises(RuntimeTaskError) as caught:
                    runner.run()
            self.assertIn("synthetic frame failure", str(caught.exception))
            self.assertFalse(
                (
                    root
                    / "failure-output"
                    / "runs"
                    / _bundle.version_id
                    / _bundle.run_id
                    / "result.json"
                ).exists()
            )

    def test_bounded_thread_pool_rejects_after_close(self) -> None:
        pool = BoundedTaskPool(1)
        future = pool.submit(lambda: 7)
        self.assertEqual(future.result(timeout=2), 7)
        pool.shutdown()
        with self.assertRaises(RuntimeError):
            pool.submit(lambda: 1)


class UiSmokeTests(unittest.TestCase):
    def test_offscreen_ui_smoke(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = dict(os.environ)
            env["QT_QPA_PLATFORM"] = "offscreen"
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "narrative_workbench",
                    "ui",
                    "--state",
                    str(SAMPLES / "state.json"),
                    "--story",
                    str(SAMPLES / "story.json"),
                    "--storyboard",
                    str(SAMPLES / "storyboard.json"),
                    "--run",
                    str(SAMPLES / "run.json"),
                    "--state-dir",
                    str(root / "ui-state"),
                    "--output-dir",
                    str(root / "ui-output"),
                    "--offscreen",
                    "--smoke-test",
                ],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(
                (
                    root
                    / "ui-output"
                    / "runs"
                    / load_bundle(**sample_paths()).version_id
                    / load_bundle(**sample_paths()).run_id
                    / "result.json"
                ).exists()
            )


if __name__ == "__main__":
    unittest.main()

