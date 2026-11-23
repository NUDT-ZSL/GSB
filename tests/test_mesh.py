from __future__ import annotations

import unittest

import numpy as np

from sceneopt.errors import E_FACE_BUDGET_EXCEEDED, FaceBudgetExceeded
from sceneopt.mesh import simplify_mesh


class MeshTests(unittest.TestCase):
    def setUp(self) -> None:
        self.vertices = np.asarray(
            (
                (0.0, 0.0, 0.0),
                (1.0, 0.0, 0.0),
                (0.0, 1.0, 0.0),
                (1.0, 1.0, 0.0),
            ),
            dtype=np.float64,
        )
        self.faces = np.asarray(((0, 1, 3), (0, 3, 2)), dtype=np.int64)
        self.uv = np.asarray(
            ((0.0, 0.0), (1.0, 0.0), (0.0, 1.0), (1.0, 1.0)),
            dtype=np.float64,
        )

    def test_equal_cost_edge_uses_ascending_vertex_indices(self) -> None:
        first = simplify_mesh(
            self.vertices, self.faces, self.uv, 1, scene_id="tie-scene"
        )
        second = simplify_mesh(
            self.vertices, self.faces, self.uv, 1, scene_id="tie-scene"
        )
        self.assertEqual(first.history[0]["edge"], [0, 1])
        self.assertEqual(first.vertices.tobytes(), second.vertices.tobytes())
        self.assertEqual(first.faces.tobytes(), second.faces.tobytes())
        self.assertLessEqual(first.faces_after, 1)

    def test_unreachable_budget_reports_scene_and_overflow(self) -> None:
        with self.assertRaises(FaceBudgetExceeded) as context:
            simplify_mesh(
                self.vertices,
                self.faces,
                self.uv,
                0,
                scene_id="small-scene",
            )
        error = context.exception
        self.assertEqual(error.code, E_FACE_BUDGET_EXCEEDED)
        self.assertEqual(error.details["scene_id"], "small-scene")
        self.assertEqual(error.details["over_budget_faces"], 1)


if __name__ == "__main__":
    unittest.main()

