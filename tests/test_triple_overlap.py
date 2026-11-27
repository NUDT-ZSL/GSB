import unittest

import numpy as np

from domecalib.core.engine import CalibrationEngine
from domecalib.core.models import ProjectorSpec
from domecalib.core.samplegen import generate_sample_project


def _ring_projector(index: int, count: int) -> ProjectorSpec:
    az = 2.0 * np.pi * index / count
    position = (3.0 * np.cos(az), 3.0 * np.sin(az), 11.0)
    return ProjectorSpec(
        projector_id=f"T{index + 1}",
        position=tuple(float(v) for v in position),
        forward=tuple(float(v) for v in (-np.asarray(position))),
        up_hint=(0.0, 0.0, 1.0),
        fov_h_deg=128.0,
        width=256,
        height=192,
        k1=-0.01,
        k2=0.002,
        calib_frac=0.18,
    )


class TripleOverlapTests(unittest.TestCase):
    def test_six_projectors_produce_triple_cells_without_jumps(self):
        specs = [_ring_projector(i, 6) for i in range(6)]
        project = generate_sample_project(projector_specs=specs)
        engine = CalibrationEngine(project)
        report = engine.full_rebuild()

        bright = report["brightness"]
        self.assertGreater(bright["triple_overlap_cell_count"], 0)
        self.assertLess(bright["uniformity_cv_post"], 1e-9)
        self.assertLess(bright["max_seam_jump"], 1e-9)
        post = bright["weight_sum_post_optimization"]
        self.assertAlmostEqual(post["min"], 1.0, places=6)
        self.assertAlmostEqual(post["max"], 1.0, places=6)


if __name__ == "__main__":
    unittest.main()
