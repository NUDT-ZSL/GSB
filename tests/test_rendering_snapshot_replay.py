import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ieditor.cli import run_replay_batch
from ieditor.config import (load_quant_rules, load_render_config,
                            load_version_map)
from ieditor.converter import save_project, upgrade_v1_to_v2
from ieditor.diagnostics import compare_renders
from ieditor.editor import edit_mask_rect, set_channel_weight
from ieditor.errors import E_SNAPSHOT_STALE, SnapshotStale
from ieditor.fingerprint import structure_fingerprint
from ieditor.images import load_rgb
from ieditor.inpaint import inpaint
from ieditor.parsers import parse_v1, parse_v2
from ieditor.pipeline import render_project
from ieditor.snapshot import (load_snapshot, read_progress, write_snapshot)
from ieditor.style import apply_style

CONFIG = ROOT / "config"
SAMPLES = ROOT / "samples"


class RenderingTests(unittest.TestCase):
    def setUp(self):
        self.config = load_render_config(CONFIG / "render_config.json")
        self.version_map = load_version_map(CONFIG / "version_map.json")

    def test_inpaint_deterministic_across_runs(self):
        project = parse_v2(SAMPLES / "sample_a_v2.json")
        image = load_rgb(SAMPLES / project.image_path)
        first = inpaint(project, image, self.config)
        second = inpaint(project, image, self.config)
        self.assertTrue(np.array_equal(first.image, second.image))
        self.assertEqual(first.filled_pixel_count,
                         int(np.count_nonzero(project.mask == 255)))
        self.assertGreater(len(first.layers), 0)
        total = sum(layer.pixel_count for layer in first.layers)
        self.assertEqual(total, first.filled_pixel_count)

    def test_style_deterministic_and_lut_metrics(self):
        project = parse_v2(SAMPLES / "sample_a_v2.json")
        image = load_rgb(SAMPLES / project.image_path)
        ref = load_rgb(SAMPLES / project.style_ref_path)
        inpainted = inpaint(project, image, self.config).image
        r1 = apply_style(project, inpainted, ref, self.config)
        r2 = apply_style(project, inpainted, ref, self.config)
        self.assertTrue(np.array_equal(r1.image, r2.image))
        self.assertEqual(r1.table_size, self.config.quant_levels ** 3)
        self.assertGreater(r1.build_time_ms, 0.0)
        self.assertGreater(r1.distinct_input_buckets, 0)
        self.assertEqual(sum(r1.hit_counts.values()),
                         project.mask.shape[0] * project.mask.shape[1])

    def test_cross_version_diagnostics(self):
        v2 = parse_v2(SAMPLES / "sample_c_v2.json")
        v1 = parse_v1(SAMPLES / "sample_c_v1.ied")
        a = render_project(v2, SAMPLES / "sample_c_v2.json",
                           self.config, version_map=self.version_map)
        b = render_project(v1, SAMPLES / "sample_c_v1.ied",
                           self.config, version_map=self.version_map)
        report = compare_renders(v2, v1, a, b, self.config,
                                 self.version_map)
        self.assertEqual(report["business_code"], "E_OK")
        self.assertIn("max_channel_diff", report)
        self.assertIn("fingerprints", report)
        self.assertEqual(report["mask_coverage"]["xor_pixels"], 0)
        self.assertIsInstance(report["version_compat_warning"], bool)


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.version_map = load_version_map(CONFIG / "version_map.json")
        self.config = load_render_config(CONFIG / "render_config.json")

    def test_snapshot_hit_and_stale_rejection(self):
        project = parse_v2(SAMPLES / "sample_a_v2.json")
        with tempfile.TemporaryDirectory() as tmp:
            write_snapshot(project, SAMPLES / "sample_a_v2.json",
                           Path(tmp), global_version=1)
            snapshot = load_snapshot(project, Path(tmp))
            self.assertEqual(snapshot.global_version, 1)

            tampered = edit_mask_rect(project, 1, 1, 3, 3, 255)
            with self.assertRaises(SnapshotStale) as ctx:
                load_snapshot(tampered, Path(tmp))
            self.assertEqual(ctx.exception.code, E_SNAPSHOT_STALE)

    def test_progress_records_last_seq(self):
        project = parse_v2(SAMPLES / "sample_a_v2.json")
        edited = edit_mask_rect(project, 2, 2, 4, 4, 255)
        with tempfile.TemporaryDirectory() as tmp:
            write_snapshot(edited, SAMPLES / "sample_a_v2.json",
                           Path(tmp), global_version=3)
            from ieditor.snapshot import write_progress
            write_progress(Path(tmp), edited.project_id,
                           edited.edit_log[-1]["seq"], 3)
            progress = read_progress(Path(tmp), edited.project_id)
            self.assertEqual(progress["last_completed_op_seq"], 0)
            self.assertEqual(progress["snapshot_global_version"], 3)


class ReplayTests(unittest.TestCase):
    def test_replay_reproducible_from_clean_workspaces(self):
        sequence = ROOT / "config" / "replay_sequence.json"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "samples").symlink_to(SAMPLES, target_is_directory=True)
            (root / "config").symlink_to(CONFIG, target_is_directory=True)
            work_a = root / "work_a"
            work_b = root / "work_b"
            r1 = run_replay_batch(root, sequence, work_a,
                                  root / "config", False)
            r2 = run_replay_batch(root, sequence, work_b,
                                  root / "config", False)
            self.assertEqual(
                r1["rendered_fingerprints"], r2["rendered_fingerprints"])
            self.assertEqual(r1["failures"], r2["failures"])
            codes = {f["code"] for f in r1["failures"]}
            self.assertEqual(
                codes,
                {"E_FORMAT_INVALID", "E_VERSION_UNSUPPORTED"})
            report_a = (work_a / "diagnose_a.json").read_bytes()
            report_b = (work_b / "diagnose_a.json").read_bytes()
            self.assertEqual(report_a, report_b)

    def test_replay_resume_skips_completed_ops(self):
        sequence = ROOT / "config" / "replay_sequence.json"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "samples").symlink_to(SAMPLES, target_is_directory=True)
            (root / "config").symlink_to(CONFIG, target_is_directory=True)
            work = root / "work"
            run_replay_batch(root, sequence, work, root / "config", False)
            progress = read_progress(work, "sample_a_v1")
            self.assertGreaterEqual(progress["snapshot_global_version"], 1)
            resumed = run_replay_batch(root, sequence, work,
                                       root / "config", True)
            # Resume must not re-apply operations before the progress marker.
            indexes = [a["index"] for a in resumed["applied"]]
            self.assertTrue(all(i > progress["last_completed_op_seq"]
                                for i in indexes))


if __name__ == "__main__":
    unittest.main(verbosity=2)
