import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from dungeon_render import persistence
from dungeon_render.errors import DungeonError
from dungeon_render.pipeline import GRAY_OUTPUT, OUTLINE_OUTPUT, run_pipeline
from dungeon_render.raster import Renderer
from tests.test_validation import make_level, make_task
from dungeon_render.validation import parse_level, parse_task


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        level = parse_level(make_level())
        task = parse_task(make_task(), level)
        self.level = level
        self.task = replace(
            task,
            state_dir=str(Path(self.tmp) / "state"),
            output_dir=str(Path(self.tmp) / "output"),
        )

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_full_run_matches_whole_image_reference(self):
        result = run_pipeline(self.level, self.task, capture_signals=False)
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(
            result["report"]["whole_image"],
            {
                "gray_max_delta": 0,
                "outline_max_delta": 0,
                "first_mismatch": None,
            },
        )
        self.assertTrue((Path(self.task.output_dir) / GRAY_OUTPUT).exists())
        self.assertTrue((Path(self.task.output_dir) / OUTLINE_OUTPUT).exists())

    def test_second_run_reuses_all_tiles(self):
        run_pipeline(self.level, self.task, capture_signals=False)
        result = run_pipeline(self.level, self.task, capture_signals=False)
        self.assertTrue(
            all(tile["status"] == "reused" for tile in result["report"]["tiles"])
        )

    def test_concurrency_changes_keep_pixels_and_plan(self):
        plans = []
        for concurrency in (1, 2, 4):
            task = replace(
                self.task,
                concurrency=concurrency,
                state_dir=str(Path(self.tmp) / f"state_{concurrency}"),
                output_dir=str(Path(self.tmp) / f"output_{concurrency}"),
            )
            result = run_pipeline(self.level, task, capture_signals=False)
            plans.append(result["round_plan"])
            self.assertEqual(
                result["report"]["whole_image"]["gray_max_delta"], 0
            )
        self.assertEqual(plans[0], plans[1])
        self.assertEqual(plans[1], plans[2])

    def test_interrupt_marker_then_resume(self):
        # Key the stop signal off persisted tile files so it flips only after
        # real tile boundaries have been crossed.
        def file_based_stop():
            state = Path(self.task.state_dir)
            done = len(list(state.glob("tile_*_gray.npy"))) if state.exists() else 0
            return done >= 3

        interrupted = run_pipeline(
            self.level, self.task, stop_event=file_based_stop, capture_signals=False
        )
        self.assertTrue(interrupted["interrupted"])
        self.assertEqual(interrupted["exit_code"], 130)
        marker = Path(self.task.state_dir) / persistence.INTERRUPT_FILE
        self.assertTrue(marker.exists())
        self.assertFalse((Path(self.task.output_dir) / GRAY_OUTPUT).exists())

        resumed = run_pipeline(self.level, self.task, capture_signals=False)
        self.assertEqual(resumed["exit_code"], 0)
        self.assertFalse(marker.exists())
        self.assertEqual(
            resumed["report"]["whole_image"]["gray_max_delta"], 0
        )

    def test_digest_failure_is_repaired_and_recorded(self):
        run_pipeline(self.level, self.task, capture_signals=False)
        gray_files = sorted(Path(self.task.state_dir).glob("tile_*_gray.npy"))
        target = gray_files[2]
        raw = bytearray(target.read_bytes())
        raw[-1] ^= 0xFF
        target.write_bytes(bytes(raw))
        result = run_pipeline(self.level, self.task, capture_signals=False)
        self.assertIn("E_DIGEST", result["business_codes"])
        self.assertEqual(
            result["report"]["whole_image"]["gray_max_delta"], 0
        )

    def test_seam_failure_reports_first_mismatch(self):
        result = run_pipeline(self.level, self.task, capture_signals=False)
        # Sabotage the published gray image, then run the seam logic directly
        # by rebuilding a report from a tampered canvas.
        from dungeon_render.report import build_report, raise_if_seam_failure
        from dungeon_render.tiles import build_tiles

        renderer = Renderer(self.level, self.task)
        ref_gray, ref_outline = renderer.render_whole()
        tampered = ref_gray.copy()
        tampered[0, 3] = min(255, int(tampered[0, 3]) + 50)
        tile_status = {
            i: {"status": "rendered", "invalid_reason": None}
            for i in range(self.task.tile_cols * self.task.tile_rows)
        }
        report = build_report(
            self.task,
            build_tiles(self.task),
            tile_status,
            tampered,
            ref_outline,
            ref_gray,
            ref_outline,
            business_codes=[],
            archived_versions=[],
        )
        with self.assertRaises(DungeonError) as caught:
            raise_if_seam_failure(report)
        self.assertEqual(caught.exception.code, "E_SEAM")
        self.assertEqual(
            report["whole_image"]["first_mismatch"]["pixel"], [3, 0]
        )


if __name__ == "__main__":
    unittest.main()
