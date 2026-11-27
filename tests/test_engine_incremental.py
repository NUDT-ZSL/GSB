import tempfile
import unittest
from pathlib import Path

import numpy as np

from domecalib.core.engine import CalibrationEngine
from domecalib.core.errors import CalibrationError, E_VERSION_CONFLICT
from domecalib.core.persistence import (
    save_snapshot,
    load_snapshot,
    list_snapshots,
    save_project,
    load_project,
)
from domecalib.core.samplegen import generate_sample_project


class IncrementalAndVersionTests(unittest.TestCase):
    def test_incremental_equals_full_pixelwise(self):
        base = generate_sample_project()
        inc = CalibrationEngine(generate_sample_project())
        inc.rebuild_luts()
        inc.rebuild_blending()

        target = base.projectors[0].projector_id
        inc.update_projector(target, fov_h_deg=base.projectors[0].fov_h_deg + 1.5)

        full = CalibrationEngine(inc.clone().project)
        full.full_rebuild()

        for pid in full.project.luts:
            a, b = inc.project.luts[pid], full.project.luts[pid]
            np.testing.assert_array_equal(a.valid, b.valid)
            for name in ("lon", "lat", "raw_lon", "raw_lat"):
                np.testing.assert_allclose(
                    getattr(a, name)[a.valid],
                    getattr(b, name)[b.valid],
                    atol=1e-12, rtol=0.0,
                )
            np.testing.assert_allclose(
                inc.project.masks[pid].weight,
                full.project.masks[pid].weight,
                atol=1e-12,
            )

    def test_only_changed_projector_version_bumps(self):
        engine = CalibrationEngine(generate_sample_project())
        engine.full_rebuild()
        before = dict(engine.project.projector_versions)
        engine.update_projector("P2", k1=-0.04)
        after = engine.project.projector_versions
        self.assertEqual(after["P2"], before["P2"] + 1)
        for pid in ("P1", "P3", "P4"):
            self.assertEqual(after[pid], before[pid])

    def test_snapshot_roundtrip_and_stale_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = CalibrationEngine(generate_sample_project())
            engine.full_rebuild()
            save_snapshot(engine.project, tmp, "v1")
            self.assertEqual([s["label"] for s in list_snapshots(tmp)], ["v1"])
            restored = load_snapshot(tmp, "v1")
            pid = "P1"
            np.testing.assert_allclose(
                restored.luts[pid].lon[restored.luts[pid].valid],
                engine.project.luts[pid].lon[engine.project.luts[pid].valid],
            )
            # Tamper expected versions -> stale data must be rejected.
            expected = dict(restored.projector_versions)
            expected["P1"] += 1
            with self.assertRaises(CalibrationError) as ctx:
                load_snapshot(tmp, "v1", expected_versions=expected)
            self.assertEqual(ctx.exception.code, E_VERSION_CONFLICT)

    def test_project_json_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = generate_sample_project()
            path = save_project(project, Path(tmp) / "project.json")
            loaded = load_project(path)
            self.assertEqual(loaded.seed, project.seed)
            self.assertEqual(len(loaded.projectors), len(project.projectors))
            np.testing.assert_array_equal(
                loaded.control_points[0].observed_lon,
                project.control_points[0].observed_lon,
            )

    def test_resolution_mismatch_diagnosed(self):
        engine = CalibrationEngine(generate_sample_project())
        engine.full_rebuild()
        lut = engine.project.luts["P1"]
        with self.assertRaises(CalibrationError) as ctx:
            lut.check_resolution(lut.width + 10, lut.height)
        self.assertEqual(ctx.exception.code, "E1005_RESOLUTION_MISMATCH")


if __name__ == "__main__":
    unittest.main()
