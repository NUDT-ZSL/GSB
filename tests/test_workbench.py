from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import numpy as np

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from medseg_workbench.cli import main as cli_main
from medseg_workbench.config import parse_task
from medseg_workbench.errors import WorkbenchError
from medseg_workbench.evaluation import export_mask
from medseg_workbench.geometry import chunk_grid
from medseg_workbench.project import INTERRUPT_EXIT_CODE, Project
from medseg_workbench.sample_data import generate_sample
from medseg_workbench.scheduler import LogicalClock, plan_rounds
from medseg_workbench.segmentation import buffer_slot_estimate, logical_cost_ms


def write_small_task(root: Path, **overrides) -> Path:
    shape = overrides.pop("shape", (8, 16, 16))
    chunk_size = overrides.pop("chunk_size", (4, 8, 8))
    rng = np.random.default_rng(1234)
    volume = rng.integers(0, 70, size=shape, dtype=np.uint8)
    volume[2:6, 4:12, 4:12] = rng.integers(140, 210, size=(4, 8, 8), dtype=np.uint8)
    volume[3:5, 10:15, 10:15] = 180
    volume_dir = root / "volume"
    volume_dir.mkdir(parents=True, exist_ok=True)
    truth_dir = root / "truth"
    truth_dir.mkdir(parents=True, exist_ok=True)
    truth = np.zeros(shape, dtype=np.uint8)
    truth[2:6, 4:12, 4:12] = 1
    truth[3:5, 10:15, 10:15] = 1
    for z in range(shape[0]):
        np.save(volume_dir / f"slice_{z:04d}.npy", volume[z])
        np.save(truth_dir / f"slice_{z:04d}.npy", truth[z])
    task = {
        "schema_version": "1.0",
        "seed": 1234,
        "shape": list(shape),
        "spacing": [1.0, 1.0, 1.0],
        "chunk_size": list(chunk_size),
        "region_growth": {"threshold": [100, 220], "seed_stride": [2, 4, 4], "growth_radius": 2},
        "morphology": {"dilate_kernel": 3, "erode_kernel": 3},
        "parallelism": 2,
        "logic_time_budget_ms": 10_000,
        "pixel_memory_budget": 10**12,
        "volume_dir": "volume",
        "truth_dir": "truth",
        "state_dir": "state",
        "temp_dir": "tmp",
        "export_dir": "exports",
    }
    task.update(overrides)
    task_path = root / "task.json"
    task_path.write_text(json.dumps(task), encoding="utf-8")
    return task_path


class KernelConsistencyTests(unittest.TestCase):
    def test_chunk_matches_full_for_parallelisms(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for workers in (1, 2, 4):
                task_path = write_small_task(
                    root / f"case-{workers}",
                    parallelism=workers,
                    state_dir=f"state-{workers}",
                )
                project = Project.from_task(task_path).prepare()
                report = project.run()
                self.assertFalse(report["interrupted"])
                assembled = project.assemble_mask()
                raw_assembled = project.assemble_mask("raw")
                full_raw, full_mask = project.full_reference()
                self.assertTrue(np.array_equal(raw_assembled, full_raw))
                self.assertTrue(np.array_equal(assembled, full_mask))
                self.assertEqual(len(report["completed_this_run"]), project.cfg.chunk_count)

    def test_boundary_edits_are_exact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_path = write_small_task(Path(tmp))
            project = Project.from_task(task_path).prepare()
            project.run()
            chunk_id = "001_001_001"
            revision = project.demo_revision(chunk_id)
            self.assertGreater(revision.edit_mask.sum(), 0)
            plan = project.invalidation_plan()
            self.assertIn(chunk_id, plan["stale_chunk_ids"])
            project.run()
            assembled = project.assemble_mask()
            _, full_mask = project.full_reference()
            self.assertTrue(np.array_equal(assembled, full_mask))


class SchedulerBudgetTests(unittest.TestCase):
    def test_deterministic_memory_parallel_and_time_admission(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_path = write_small_task(Path(tmp))
            cfg = parse_task(task_path)
            chunks = chunk_grid(cfg.shape, cfg.chunk_size)

            plan2 = plan_rounds(
                chunks,
                parallelism=2,
                memory_budget_slots=10**12,
                time_budget_ms=10**9,
                memory_cost=lambda c: 10,
                logical_cost=lambda c: 3,
            )
            self.assertEqual(plan2.admission_sequence, [c.id for c in chunks])
            self.assertTrue(all(len(r.chunks) <= 2 for r in plan2.rounds))

            plan_memory = plan_rounds(
                chunks,
                parallelism=8,
                memory_budget_slots=15,
                time_budget_ms=10**9,
                memory_cost=lambda c: 10,
                logical_cost=lambda c: 1,
            )
            self.assertTrue(all(r.memory_slots <= 15 for r in plan_memory.rounds))
            self.assertTrue(all(len(r.chunks) == 1 for r in plan_memory.rounds))

            plan_time = plan_rounds(
                chunks,
                parallelism=8,
                memory_budget_slots=10**12,
                time_budget_ms=5,
                memory_cost=lambda c: 1,
                logical_cost=lambda c: 10,
            )
            self.assertTrue(all(len(r.chunks) == 1 for r in plan_time.rounds))
            self.assertTrue(all(r.time_budget_exceeded for r in plan_time.rounds))
            again = plan_rounds(
                chunks,
                parallelism=8,
                memory_budget_slots=10**12,
                time_budget_ms=5,
                memory_cost=lambda c: 1,
                logical_cost=lambda c: 10,
            )
            self.assertEqual(plan_time.to_dict(), again.to_dict())

    def test_logical_clock_is_injectable(self) -> None:
        clock = LogicalClock()
        clock.advance(12.5)
        self.assertEqual(clock.now(), 12.5)


class ValidationErrorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.task_path = write_small_task(self.root / "valid")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def invalid_cli(self, mutate, filename: str = "bad.json") -> dict:
        payload = json.loads(self.task_path.read_text(encoding="utf-8"))
        mutate(payload)
        bad_path = self.root / filename
        bad_path.write_text(json.dumps(payload), encoding="utf-8")
        before = sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*"))
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = cli_main(["validate", "--task", str(bad_path)])
        after = sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*"))
        self.assertEqual(code, 2)
        self.assertEqual(before, after)
        response = json.loads(stdout.getvalue())
        self.assertFalse(response["ok"])
        self.assertTrue(response["error"]["code"].startswith("E_"))
        return response

    def test_business_error_codes(self) -> None:
        cases = [
            ("E_DIMENSION", lambda p: p.update(shape=[1, 2])),
            ("E_SPACING", lambda p: p.update(spacing=[1, 0, 1])),
            ("E_CHUNK", lambda p: p.update(chunk_size=[0, 8, 8])),
            ("E_THRESHOLD", lambda p: p["region_growth"].update(threshold=[200, 100])),
            ("E_MORPHOLOGY", lambda p: p["morphology"].update(dilate_kernel=4)),
            ("E_PARALLELISM", lambda p: p.update(parallelism=0)),
            ("E_MEMORY_BUDGET", lambda p: p.update(pixel_memory_budget=0)),
        ]
        for expected, mutate in cases:
            with self.subTest(expected=expected):
                response = self.invalid_cli(mutate, expected + ".json")
                self.assertEqual(response["error"]["code"], expected)

    def test_json_and_volume_errors(self) -> None:
        bad_json = self.root / "broken.json"
        bad_json.write_text("{not json", encoding="utf-8")
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = cli_main(["validate", "--task", str(bad_json)])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(stdout.getvalue())["error"]["code"], "E_JSON")

        payload = json.loads(self.task_path.read_text())
        payload["shape"] = [8, 16, 16]
        missing_root = self.root / "missing-volume"
        missing_root.mkdir()
        bad_task = missing_root / "task.json"
        bad_task.write_text(json.dumps(payload))
        with self.assertRaises(WorkbenchError) as ctx:
            Project.from_task(bad_task)
        self.assertEqual(ctx.exception.code, "E_VOLUME")


class PersistenceRevisionRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.task_path = write_small_task(Path(self.tmp.name) / "case")
        self.project = Project.from_task(self.task_path).prepare()
        self.project.run()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_precise_invalidation_incremental_and_reuse(self) -> None:
        chunk_id = "001_001_001"
        self.project.demo_revision(chunk_id)
        plan = self.project.invalidation_plan()
        self.assertEqual(plan["stale_chunk_ids"], ["000_001_001", chunk_id])

        unrelated = "000_000_000"
        unrelated_path = self.project.chunk_path(unrelated)
        before_stat = unrelated_path.stat().st_mtime_ns
        report = self.project.run()
        self.assertEqual(sorted(report["completed_this_run"]), ["000_001_001", chunk_id])
        self.assertIn(unrelated, report["reused_chunks"])
        self.assertEqual(unrelated_path.stat().st_mtime_ns, before_stat)

        assembled = self.project.assemble_mask()
        _, full_mask = self.project.full_reference()
        self.assertTrue(np.array_equal(assembled, full_mask))

        second = self.project.run()
        self.assertEqual(second["completed_this_run"], [])
        self.assertEqual(len(second["reused_chunks"]), self.project.cfg.chunk_count)

    def test_digest_mismatch_recovers_with_e_digest(self) -> None:
        chunk_id = "000_000_000"
        path = self.project.chunk_path(chunk_id)
        path.write_bytes(b"corrupt npz payload")
        project = Project.from_task(self.task_path)
        report = project.run()
        codes = {item["chunk"]: item["code"] for item in report["digest_recoveries"]}
        self.assertEqual(codes.get(chunk_id), "E_DIGEST")
        self.assertIn(chunk_id, report["completed_this_run"])
        self.assertTrue(np.array_equal(project.assemble_mask(), project.full_reference()[1]))

    def test_final_mask_revision_is_wysiwyg_and_exact(self) -> None:
        chunk_id = "001_001_001"
        revision = self.project.demo_final_revision(chunk_id)
        self.assertTrue(revision.edit_mask.any())
        plan = self.project.invalidation_plan()
        self.assertEqual(plan["stale_chunk_ids"], ["000_001_001", chunk_id])
        report = self.project.run()
        self.assertEqual(sorted(report["completed_this_run"]), ["000_001_001", chunk_id])
        self.assertTrue(np.array_equal(self.project.assemble_mask(), self.project.full_reference()[1]))
        second = self.project.run()
        self.assertEqual(second["completed_this_run"], [])

    def test_version_change_archives_old_chunks(self) -> None:
        payload = json.loads(self.task_path.read_text(encoding="utf-8"))
        old_version = Project.from_task(self.task_path).version_id
        payload["parallelism"] = 4
        self.task_path.write_text(json.dumps(payload), encoding="utf-8")
        project = Project.from_task(self.task_path).prepare()
        self.assertNotEqual(project.version_id, old_version)
        archived = list((project.archive_dir / old_version).glob("chunks/*.npz"))
        self.assertTrue(archived)
        report = project.run()
        self.assertEqual(len(report["completed_this_run"]), project.cfg.chunk_count)
        self.assertTrue(np.array_equal(project.assemble_mask(), project.full_reference()[1]))

    def test_interrupt_then_resume(self) -> None:
        for path in self.project.chunks_dir.glob("*.npz"):
            path.unlink()
        stop = threading.Event()
        stop.set()
        report = self.project.run(stop_event=stop)
        self.assertTrue(report["interrupted"])
        self.assertEqual(report["exit_code"], INTERRUPT_EXIT_CODE)
        self.assertTrue(self.project.interrupt_path.exists())
        self.assertEqual(list(self.project.chunks_dir.glob("*.npz")), [])

        report = self.project.run()
        self.assertFalse(report["interrupted"])
        self.assertFalse(self.project.interrupt_path.exists())
        self.assertTrue(np.array_equal(self.project.assemble_mask(), self.project.full_reference()[1]))

    def test_stale_temp_and_missing_chunk_recovery(self) -> None:
        missing = self.project.chunk_path("000_000_001")
        missing.unlink()
        stale_temp = self.project.chunks_dir / "000_000_002.npz.tmp"
        stale_temp.write_bytes(b"partial")
        report = Project.from_task(self.task_path).run()
        self.assertIn("000_000_001", report["completed_this_run"])
        self.assertFalse(stale_temp.exists())
        self.assertTrue(np.array_equal(self.project.assemble_mask(), self.project.full_reference()[1]))

    def test_export_version_skips_identical_and_replaces_changes(self) -> None:
        out = self.project.cfg.export_dir / "mask.npz"
        first = export_mask(self.project, out)
        second = export_mask(self.project, out)
        self.assertEqual(first["action"], "replaced")
        self.assertEqual(second["action"], "unchanged")
        self.project.demo_revision("001_001_001")
        self.project.run()
        third = export_mask(self.project, out)
        self.assertEqual(third["action"], "replaced")


class SampleAcceptanceTests(unittest.TestCase):
    def test_generated_sample_evaluation_and_exactness(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "sample"
            generate_sample(root, force=True)
            task_path = root / "task.json"
            project = Project.from_task(task_path).prepare()
            project.run()
            self.assertTrue(np.array_equal(project.assemble_mask(), project.full_reference()[1]))

            project.demo_revision("001_001_001")
            plan = project.invalidation_plan()
            self.assertIn("001_001_001", plan["stale_chunk_ids"])
            project.run()
            self.assertTrue(np.array_equal(project.assemble_mask(), project.full_reference()[1]))


class GuiSmokeTests(unittest.TestCase):
    def test_offscreen_window(self) -> None:
        from unittest.mock import patch
        from PyQt5.QtWidgets import QApplication, QMessageBox
        from medseg_workbench.gui import MainWindow

        with tempfile.TemporaryDirectory() as tmp:
            task_path = write_small_task(Path(tmp))
            app = QApplication.instance() or QApplication(sys.argv)
            with patch.object(QMessageBox, "information", return_value=None), patch.object(
                QMessageBox, "critical", side_effect=AssertionError
            ):
                window = MainWindow(task_path)
                window.project.run()
                window.refresh()
                window.canvas.repaint()
                self.assertEqual(window.chunk_list.count(), 8)
                window.select_chunk("001_001_001")
                window.begin_stroke("001_001_001", 0)
                window.extend_stroke(4, 10, 10)
                window.commit_stroke()
                self.assertIn("001_001_001", window.invalid_ids)
                window.project.run()
                window.refresh()
                window.evaluate()
                window.export()
                window.canvas.repaint()


if __name__ == "__main__":
    unittest.main()
