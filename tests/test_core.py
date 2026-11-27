from __future__ import annotations

import unittest

import numpy as np

from signhand import (
    SDFScene,
    SkinnedHand,
    SphereTracer,
    TRACE_ERR_HIT_BEYOND_FAR_PLANE,
    TRACE_ERR_MAX_STEPS,
    TRACE_HIT,
    load_project,
)
from signhand.collision import IntersectionDetector
from signhand.math3d import inverse_rigid
from signhand.project import CameraSpec, PrimitiveSpec
from signhand.sdf import PrimitiveInstance


PROJECT = "examples/generated/sign_actions.json"


def sphere_scene(center, radius, bone="bone_a", primitive_id="sphere_a"):
    spec = PrimitiveSpec(
        id=primitive_id,
        kind="sphere",
        bone=bone,
        center=np.asarray(center, dtype=np.float64),
        radius=float(radius),
    )
    world = np.eye(4)
    return PrimitiveInstance(spec, world, inverse_rigid(world))


class CoreKernelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.project = load_project(PROJECT)
        cls.skinner = SkinnedHand(cls.project)

    def test_rest_skinning_reproduces_bind_pose(self):
        rest_world = self.skinner.sampler.rest_world_matrices
        result = self.skinner.skin_with_world(0, rest_world)
        np.testing.assert_allclose(
            result.skinned_vertices,
            self.project.mesh.vertices,
            atol=1e-12,
            rtol=0.0,
        )

    def test_keyframe_sampling_is_deterministic(self):
        first = self.skinner.skin(4)
        second = self.skinner.skin(4)
        for name in self.project.joint_order:
            np.testing.assert_array_equal(
                first.world_matrices[name], second.world_matrices[name]
            )
        np.testing.assert_array_equal(
            first.skinned_vertices, second.skinned_vertices
        )

    def test_sphere_distance_gradient_and_repeatability(self):
        instance = sphere_scene([0.0, 0.0, 0.0], 1.0)
        scene = SDFScene([instance])
        points = np.array(
            [
                [0.0, 0.0, 0.0],
                [2.0, 0.0, 0.0],
                [0.0, 0.5, 0.0],
            ]
        )
        np.testing.assert_allclose(scene.distance(points), [-1.0, 1.0, -0.5])
        first = scene.distance(points)
        second = scene.distance(points)
        np.testing.assert_array_equal(first, second)
        gradient = scene.gradient(np.array([[2.0, 0.0, 0.0]]))
        np.testing.assert_allclose(gradient, [[1.0, 0.0, 0.0]], atol=1e-7)

    def _camera(self, far=10.0, position=(0.0, 0.0, 0.0), target=(0.0, 0.0, 1.0)):
        return CameraSpec(
            mode="orthographic",
            position=np.asarray(position, dtype=np.float64),
            target=np.asarray(target, dtype=np.float64),
            up=np.array([0.0, 1.0, 0.0]),
            near=0.1,
            far=far,
            half_width=1.0,
            half_height=1.0,
            fov_deg=45.0,
        )

    def test_sphere_trace_hit_and_normal(self):
        scene = SDFScene([sphere_scene([0.0, 0.0, 0.0], 1.0)])
        tracer = SphereTracer(
            scene,
            self._camera(position=(0.0, 0.0, 5.0), target=(0.0, 0.0, 0.0)),
            self.project.render,
        )
        trace = tracer.trace_rays(
            np.array([[0.0, 0.0, 5.0]]),
            np.array([[0.0, 0.0, -1.0]]),
        )
        self.assertEqual(int(trace.status[0]), TRACE_HIT)
        self.assertAlmostEqual(float(trace.hit_t[0]), 4.0, delta=2e-3)
        np.testing.assert_allclose(trace.normals[0], [0.0, 0.0, 1.0], atol=2e-3)
        repeated = tracer.trace_rays(
            np.array([[0.0, 0.0, 5.0]]),
            np.array([[0.0, 0.0, -1.0]]),
        )
        np.testing.assert_array_equal(trace.hit_t, repeated.hit_t)
        np.testing.assert_array_equal(trace.normals, repeated.normals)

    def test_trace_beyond_far_plane_status(self):
        scene = SDFScene([sphere_scene([0.0, 0.0, 20.0], 1.0)])
        tracer = SphereTracer(scene, self._camera(far=10.0), self.project.render)
        trace = tracer.trace_rays(
            np.array([[0.0, 0.0, 0.0]]),
            np.array([[0.0, 0.0, 1.0]]),
        )
        self.assertEqual(int(trace.status[0]), TRACE_ERR_HIT_BEYOND_FAR_PLANE)

    def test_trace_max_steps_status(self):
        scene = SDFScene([sphere_scene([0.0, 0.0, 5.0], 1.0)])
        from dataclasses import replace

        settings = replace(self.project.render, max_steps=4)
        tracer = SphereTracer(scene, self._camera(far=20.0), settings)
        trace = tracer.trace_rays(
            np.array([[0.0, 0.0, 0.0]]),
            np.array([[0.0, 0.0, 1.0]]),
        )
        self.assertEqual(int(trace.status[0]), TRACE_ERR_MAX_STEPS)

    def test_complete_overlap_is_detected(self):
        first = sphere_scene([0.0, 0.0, 0.0], 0.5, "bone_a", "a")
        second = sphere_scene([0.0, 0.0, 0.0], 0.5, "bone_b", "b")
        scene = SDFScene([first, second])
        detector = IntersectionDetector({"surface_samples": 64, "grid_resolution": 7})
        records = detector.detect(scene, 3, "overlap")
        self.assertEqual(len(records), 1)
        self.assertAlmostEqual(records[0]["max_penetration_depth"], 0.5, places=5)
        self.assertEqual(records[0]["frame"], 3)
        np.testing.assert_array_equal(
            np.asarray(records),
            np.asarray(detector.detect(scene, 3, "overlap")),
        )

    def test_separated_primitives_are_not_reported(self):
        first = sphere_scene([-2.0, 0.0, 0.0], 0.5, "bone_a", "a")
        second = sphere_scene([2.0, 0.0, 0.0], 0.5, "bone_b", "b")
        records = IntersectionDetector({}).detect(SDFScene([first, second]), 0)
        self.assertEqual(records, [])


if __name__ == "__main__":
    unittest.main()

