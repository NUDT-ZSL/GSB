"""端到端与各模块测试，覆盖解析、约束、轨迹、相机、平滑、确定性与核对。"""

import json
import unittest

import numpy as np

from museum_anim.camera import CameraConfig, frustum_intersects_aabb
from museum_anim.constraints import CaseConfig
from museum_anim.geometry import parse_obj
from museum_anim.parser import UnparsedEntry, parse_entry, parse_script
from museum_anim.pipeline import PipelineError, UnparsedAction, run_pipeline
from museum_anim.sample_data import (
    CAMERA_CONFIG,
    CASE_BOUNDS,
    SCRIPT_ENTRIES,
    SCRIPT_ENTRIES_WITH_INVALID,
    build_vase_obj,
)


class ParserTests(unittest.TestCase):
    def test_rotate_direction_amplitude_period_easing(self):
        parsed = parse_entry("绕Z轴缓慢旋转一周，周期8秒，线性", 0)
        self.assertEqual(parsed["direction"], "ROT_Z")
        self.assertEqual(parsed["action_type"], "ROTATE")
        self.assertEqual(parsed["amplitude"], 360.0)
        self.assertEqual(parsed["period"], 8.0)
        self.assertEqual(parsed["easing"], "LINEAR")

    def test_swing_translate_expand_enums(self):
        swing = parse_entry("沿水平轴来回摆动，幅度45度，周期4秒，缓入缓出")
        self.assertEqual(swing["direction"], "SWING_X")
        self.assertEqual(swing["amplitude"], 45.0)
        self.assertEqual(swing["easing"], "EASE_IN_OUT")

        move = parse_entry("沿Y轴平移50厘米，周期3秒，正弦")
        self.assertEqual(move["direction"], "MOVE_Y")
        self.assertAlmostEqual(move["amplitude"], 0.5)
        self.assertEqual(move["easing"], "SINE")

        expand = parse_entry("按材质分区展开外扩0.1米，周期6秒，线性")
        self.assertEqual(expand["direction"], "EXPAND")
        self.assertEqual(expand["action_type"], "EXPAND")

    def test_unparsed_entry_raises(self):
        with self.assertRaises(UnparsedEntry) as ctx:
            parse_entry("让文物显得更有故事感", 3)
        self.assertEqual(ctx.exception.index, 3)

    def test_parse_script_reports_index(self):
        with self.assertRaises(UnparsedEntry) as ctx:
            parse_script(SCRIPT_ENTRIES_WITH_INVALID)
        self.assertEqual(ctx.exception.index, 5)


class GeometryTests(unittest.TestCase):
    def test_obj_material_groups_and_aabb(self):
        model = parse_obj(build_vase_obj())
        self.assertGreaterEqual(len(model.groups), 3)
        self.assertTrue(np.all(model.aabb_max > model.aabb_min))
        self.assertEqual(model.vertex_normals.shape, model.vertices.shape)


class ConstraintTests(unittest.TestCase):
    def setUp(self):
        self.model = parse_obj(build_vase_obj())
        self.case = CaseConfig(CASE_BOUNDS)

    def test_translation_compression(self):
        small_case = CaseConfig({"min": [-0.6, -0.6, 0.0], "max": [0.6, 0.6, 2.0]})
        parsed = parse_entry("沿X轴平移2米，周期4秒，线性")
        from museum_anim.constraints import solve_action_constraint
        record = solve_action_constraint(parsed, self.model, small_case)
        self.assertEqual(record["status"], "CONSTRAINED")
        self.assertLess(abs(record["amplitude_adjusted"]), 2.0)
        self.assertLess(record["compression_ratio"], 1.0)

    def test_full_rotation_feasible(self):
        parsed = parse_entry("绕Z轴旋转一周，周期6秒，线性")
        from museum_anim.constraints import solve_action_constraint
        record = solve_action_constraint(parsed, self.model, self.case)
        self.assertEqual(record["status"], "WITHIN_BOUNDS")
        self.assertEqual(record["amplitude_adjusted"], 360.0)

    def test_model_outside_case_rejected(self):
        bad_case = CaseConfig({"min": [-0.1, -0.1, 0.0], "max": [0.1, 0.1, 0.2]})
        from museum_anim.constraints import validate_model_inside_case
        with self.assertRaises(ValueError):
            validate_model_inside_case(self.model, bad_case)


class PipelineTests(unittest.TestCase):
    def _request(self, **overrides):
        req = {
            "fps": 25,
            "obj_text": build_vase_obj(),
            "script": SCRIPT_ENTRIES,
            "case_bounds": CASE_BOUNDS,
            "camera": CAMERA_CONFIG,
        }
        req.update(overrides)
        return req

    def test_full_pipeline_verification_passes(self):
        result = run_pipeline(self._request())
        self.assertEqual(result["status"], "OK")
        self.assertEqual(len(result["clips"]), len(SCRIPT_ENTRIES))
        self.assertTrue(result["verification_report"]["all_passed"])
        for clip in result["clips"]:
            self.assertEqual(len(clip["model_keyframes"]), len(clip["camera_keyframes"]))
            self.assertTrue(all(k["frustum_clear"] for k in clip["camera_keyframes"]))

    def test_determinism_bit_identical(self):
        req = self._request()
        first = json.dumps(run_pipeline(req), sort_keys=True)
        second = json.dumps(run_pipeline(req), sort_keys=True)
        self.assertEqual(first, second)

    def test_unparsed_business_error(self):
        req = self._request(script=SCRIPT_ENTRIES_WITH_INVALID)
        with self.assertRaises(UnparsedAction) as ctx:
            run_pipeline(req)
        self.assertEqual(ctx.exception.code, "E_UNPARSED")
        self.assertEqual(ctx.exception.index, 5)

    def test_missing_input_error(self):
        with self.assertRaises(PipelineError):
            run_pipeline({"fps": 25})

    def test_timestamps_aligned_and_fixed_rate(self):
        result = run_pipeline(self._request())
        clip = result["clips"][0]
        model_t = [k["timestamp"] for k in clip["model_keyframes"]]
        camera_t = [k["timestamp"] for k in clip["camera_keyframes"]]
        self.assertTrue(np.allclose(model_t, camera_t))
        deltas = np.diff(model_t)
        self.assertTrue(np.allclose(deltas, deltas[0]))

    def test_summary_contains_required_fields(self):
        result = run_pipeline(self._request())
        action = result["summary_report"]["actions"][0]
        for field in ("direction", "amplitude_adjusted", "period",
                      "boundary_occupancy_ratio", "model_smoothness",
                      "camera_smoothness", "constraint_status"):
            self.assertIn(field, action)

    def test_resampling_reduces_acceleration_spike(self):
        req = self._request(
            script=["沿X轴平移0.8米，周期4秒，缓入缓出"],
            smoothness_thresholds={
                "model_max_velocity": 0.15, "model_max_acceleration": 0.2,
                "camera_max_velocity": 1000.0, "camera_max_acceleration": 1000.0,
            },
        )
        result = run_pipeline(req)
        summary = result["summary_report"]["actions"][0]
        self.assertTrue(summary["resampling"]["resampled"])
        self.assertEqual(summary["model_smoothness"]["over_acceleration_frames"], 0)
        self.assertLessEqual(
            summary["model_smoothness"]["max_acceleration"],
            summary["resampling"]["before"]["model_max_acceleration"],
        )


class CameraFrustumTests(unittest.TestCase):
    def test_frustum_does_not_interact_internal_aabb(self):
        # 视锥外的 AABB 应判定为不相交
        cam = CameraConfig(CAMERA_CONFIG)
        position = np.array([0.0, -5.0, 2.0])
        forward = np.array([0.0, 1.0, -0.4])
        from museum_anim.camera import _view_rotation
        rotation = _view_rotation(forward, np.array([0.0, 0.0, 1.0]))
        aabb_min = np.array([5.0, 5.0, 5.0])
        aabb_max = aabb_min + 0.5
        self.assertFalse(frustum_intersects_aabb(position, rotation, cam,
                                                 aabb_min, aabb_max))


if __name__ == "__main__":
    unittest.main()
