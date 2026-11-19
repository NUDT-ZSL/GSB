"""工作台测试套件（unittest，仅依赖 NumPy）。

运行: python -m unittest discover -s tests -v
"""

import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from rope_workbench import attention, rope, samples, validate, variants, verify
from rope_workbench.config import RunConfig
from rope_workbench.errors import (
    E_ANGLE_OVERFLOW, E_DUP_CASE, E_DTYPE_UNSUPPORTED, E_INJECT_ANGLE,
    E_INJECT_NORM, E_INJECT_ROTATE, E_INJECT_SCORE, E_PARAM_INVALID,
    E_RESOURCE_LIMIT, E_SHAPE_INVALID, E_VARIANT_MISMATCH, E_VERIFY_FAIL,
)
from rope_workbench.pipeline import run_case
from rope_workbench.report import result_hash


def make_case(**kw):
    case = dict(case_id="t001", seq_len=32, head_dim=8, num_heads=2,
                base_theta=10000.0, pos_encoding_variant="rope_classic",
                pos_scale=1.0, block_size=8, causal=True, seed=7)
    case.update(kw)
    return case


class TestRope(unittest.TestCase):
    def test_angle_table_deterministic_bytes(self):
        case = make_case()
        a1, _ = rope.angle_table(case, RunConfig())
        a2, _ = rope.angle_table(case, RunConfig())
        self.assertEqual(a1.tobytes(), a2.tobytes())

    def test_angle_values_classic(self):
        case = make_case(seq_len=4, head_dim=4)
        angles, _ = rope.angle_table(case, RunConfig())
        inv = 10000.0 ** (-2.0 * np.arange(2) / 4)
        expect = np.arange(4)[:, None] * inv[None, :]
        np.testing.assert_allclose(angles, expect, rtol=1e-12)

    def test_interpolation_scaling(self):
        c1 = make_case()
        c2 = make_case(pos_encoding_variant="position_interpolation",
                       pos_scale=4.0)
        a1, _ = rope.angle_table(c1, RunConfig())
        a2, _ = rope.angle_table(c2, RunConfig())
        np.testing.assert_allclose(a2, a1 / 4.0, rtol=1e-12)

    def test_rotation_preserves_norm(self):
        case = make_case()
        angles, _ = rope.angle_table(case, RunConfig())
        x = np.random.default_rng(0).standard_normal((2, 32, 8))
        y = rope.apply_rotation(x, angles, "t001", 8)
        np.testing.assert_allclose(
            np.linalg.norm(y, axis=-1), np.linalg.norm(x, axis=-1), rtol=1e-12)

    def test_angle_overflow(self):
        case = make_case(seq_len=1000)
        cfg = RunConfig(angle_overflow_threshold=100.0)
        with self.assertRaises(Exception) as ctx:
            rope.angle_table(case, cfg)
        self.assertEqual(ctx.exception.code, E_ANGLE_OVERFLOW)

    def test_subnormal_guard_marks_diag(self):
        case = make_case(head_dim=2, base_theta=1e12, seq_len=4)
        cfg = RunConfig(subnormal_guard=True)
        _, diag = rope.angle_table(case, cfg)
        self.assertIn("subnormal_guard_triggered", diag)


class TestAttention(unittest.TestCase):
    def test_blocked_matches_full(self):
        rng = np.random.default_rng(1)
        q = rng.standard_normal((2, 37, 8))
        k = rng.standard_normal((2, 37, 8))
        v = rng.standard_normal((2, 37, 8))
        for causal in (True, False):
            for bs in (1, 5, 8, 37):
                blocks = attention.score_blocks(q, k, bs, causal)
                out, _ = attention.normalize_blocks(blocks, v, 37)
                ref = attention.full_attention(q, k, v, causal)
                dev = float(np.max(np.abs(out - ref)))
                self.assertLess(dev, 1e-12, f"bs={bs} causal={causal}")

    def test_causal_no_future_access(self):
        rng = np.random.default_rng(2)
        q = rng.standard_normal((1, 16, 4))
        k = rng.standard_normal((1, 16, 4))
        v = rng.standard_normal((1, 16, 4))
        out = attention.full_attention(q, k, v, True)
        v2 = v.copy()
        v2[0, 8:, :] = 999.0
        out2 = attention.full_attention(q, k, v2, True)
        np.testing.assert_allclose(out[0, :8], out2[0, :8], atol=0.0)


class TestInjection(unittest.TestCase):
    def test_angle_injection(self):
        r = run_case(make_case(pos_encoding_variant="position_interpolation",
                               pos_scale=-1.0))
        self.assertEqual(r.status, "degraded")
        self.assertEqual(r.injections[0]["code"], E_INJECT_ANGLE)
        self.assertEqual(r.injections[0]["stage"], "angle")
        self.assertEqual(r.degradations[0]["degradation_class"],
                         "identity_angles")
        self.assertEqual(r.degradations[0]["upstream_code"], E_INJECT_ANGLE)
        self.assertTrue(np.all(r.angles == 0.0))

    def test_rotate_injection_odd_dim(self):
        r = run_case(make_case(head_dim=7))
        self.assertEqual(r.injections[0]["code"], E_INJECT_ROTATE)
        self.assertEqual(r.degradations[0]["degradation_class"],
                         "skip_rotation")

    def test_score_injection_block_too_big(self):
        r = run_case(make_case(block_size=128))
        self.assertEqual(r.injections[0]["code"], E_INJECT_SCORE)
        self.assertEqual(r.degradations[0]["degradation_class"], "clamp_block")

    def test_norm_injection_mask_conflict(self):
        r = run_case(make_case(causal=False, mask="causal"))
        self.assertEqual(r.injections[0]["code"], E_INJECT_NORM)
        self.assertEqual(r.degradations[0]["degradation_class"],
                         "reuse_prev_stats")
        modes = {s["mode"] for s in r.norm_stats}
        self.assertIn("uniform_weights", modes)
        self.assertIn("reuse_prev_stats", modes)

    def test_declared_injection(self):
        r = run_case(make_case(inject={"stage": "score", "kind": "fuzz"}))
        self.assertEqual(r.injections[0]["stage"], "score")
        self.assertEqual(r.injections[0]["trigger"], "declared")

    def test_no_silent_pass(self):
        r = run_case(make_case(pos_scale=-2.0,
                               pos_encoding_variant="position_interpolation"))
        self.assertNotEqual(r.status, "ok")
        self.assertTrue(r.injections)
        self.assertTrue(r.degradations)


class TestValidation(unittest.TestCase):
    def test_dup_case(self):
        cases = [make_case(case_id="x"), make_case(case_id="x")]
        errors = validate.validate_manifest(cases)
        self.assertEqual(errors[0]["code"], E_DUP_CASE)
        self.assertEqual(errors[0]["field"], "case_id")

    def test_param_invalid(self):
        errors = validate.validate_case_params(make_case(head_dim=0))
        self.assertEqual(errors[0]["code"], E_PARAM_INVALID)
        self.assertEqual(errors[0]["field"], "head_dim")
        errors = validate.validate_case_params(make_case(base_theta=0.5))
        self.assertEqual(errors[0]["code"], E_PARAM_INVALID)
        self.assertEqual(errors[0]["field"], "base_theta")

    def test_shape_invalid(self):
        case = make_case()
        q, k, v = samples.case_arrays(case)
        errors = validate.validate_arrays(case, q[:, :-1], k, v)
        self.assertEqual(errors[0]["code"], E_SHAPE_INVALID)
        self.assertEqual(errors[0]["field"], "q")

    def test_dtype_unsupported(self):
        case = make_case()
        q, k, v = samples.case_arrays(case)
        errors = validate.validate_arrays(case, q.astype(np.int32), k, v)
        self.assertEqual(errors[0]["code"], E_DTYPE_UNSUPPORTED)

    def test_resource_limit_fail(self):
        cfg = RunConfig(max_seq_len=8)
        r = run_case(make_case(seq_len=32), config=cfg)
        self.assertEqual(r.status, "rejected")
        self.assertEqual(r.errors[0]["code"], E_RESOURCE_LIMIT)

    def test_resource_limit_degrade(self):
        cfg = RunConfig(max_seq_len=16, resource_policy="degrade")
        r = run_case(make_case(seq_len=32), config=cfg)
        self.assertEqual(r.degradations[0]["degradation_class"],
                         "resource_truncate")
        self.assertEqual(r.output.shape[1], 16)

    def test_block_resident_limit(self):
        cfg = RunConfig(max_block_resident_bytes=1024)
        r = run_case(make_case(), config=cfg)
        self.assertEqual(r.errors[0]["code"], E_RESOURCE_LIMIT)
        self.assertEqual(r.errors[0]["field"], "block_size")


class TestVariants(unittest.TestCase):
    def test_equivalent_variants_pass(self):
        c1 = make_case(case_id="a")
        c2 = make_case(case_id="b",
                       pos_encoding_variant="position_interpolation",
                       pos_scale=1.0, expect_equivalent=True)
        r1, r2 = run_case(c1), run_case(c2)
        rep = variants.adjudicate(r1, r2, RunConfig())
        self.assertEqual(rep["errors"], [])
        self.assertLess(rep["angle_dev_spectrum"]["max_abs"], 1e-12)

    def test_variant_mismatch(self):
        c1 = make_case(case_id="a")
        c2 = make_case(case_id="b",
                       pos_encoding_variant="position_interpolation",
                       pos_scale=2.0, expect_equivalent=True)
        r1, r2 = run_case(c1), run_case(c2)
        cfg = RunConfig()
        rep = variants.adjudicate(r1, r2, cfg)
        self.assertEqual(rep["errors"], [])  # 按 /s 映射应一致
        r2.angles = r2.angles * 1.5           # 篡改后必须报 mismatch
        rep = variants.adjudicate(r1, r2, cfg)
        self.assertEqual(rep["errors"][0]["code"], E_VARIANT_MISMATCH)
        self.assertIn("first_dev_pos", rep["errors"][0]["detail"])
        self.assertEqual(rep["errors"][0]["detail"]["variant_b"],
                         "position_interpolation")

    def test_ntk_mapping(self):
        c1 = make_case(case_id="a")
        c3 = make_case(case_id="c", pos_encoding_variant="ntk_aware",
                       pos_scale=2.0)
        r1, r3 = run_case(c1), run_case(c3)
        rep = variants.adjudicate(r1, r3, RunConfig())
        self.assertLess(rep["angle_dev_spectrum"]["max_abs"], 1e-9)


class TestVerify(unittest.TestCase):
    def test_verify_pass(self):
        case = make_case()
        r = run_case(case)
        issues = verify.verify_case(case, r, RunConfig())
        self.assertEqual(issues, [])

    def test_verify_detects_angle_tamper(self):
        case = make_case()
        r = run_case(case)
        r.angles = r.angles.copy()
        r.angles[3, 1] += 1.0
        issues = verify.verify_case(case, r, RunConfig())
        self.assertEqual(issues[0]["code"], E_VERIFY_FAIL)
        self.assertEqual(issues[0]["detail"]["first_dev_pos"], 3)

    def test_verify_detects_output_tamper(self):
        case = make_case()
        r = run_case(case)
        r.output = r.output.copy()
        r.output[0, 0, 0] += 1.0
        issues = verify.verify_case(case, r, RunConfig())
        self.assertTrue(any(i["code"] == E_VERIFY_FAIL for i in issues))

    def test_verify_injection_cases(self):
        for kw in (dict(pos_scale=-1.0,
                        pos_encoding_variant="position_interpolation"),
                   dict(head_dim=7),
                   dict(block_size=128),
                   dict(causal=False, mask="causal")):
            case = make_case(**kw)
            r = run_case(case)
            issues = verify.verify_case(case, r, RunConfig())
            self.assertEqual(issues, [], f"{kw}: {issues}")


class TestReproducibility(unittest.TestCase):
    def test_hash_rerun_identical(self):
        case = make_case()
        h1 = result_hash(run_case(case))
        h2 = result_hash(run_case(case))
        self.assertEqual(h1, h2)

    def test_all_builtin_cases_hash_stable(self):
        for case in samples.default_cases():
            h1 = result_hash(run_case(case))
            h2 = result_hash(run_case(case))
            self.assertEqual(h1, h2, case["case_id"])

    def test_sample_arrays_deterministic(self):
        case = make_case()
        q1, k1, v1 = samples.case_arrays(case)
        q2, k2, v2 = samples.case_arrays(case)
        self.assertEqual(q1.tobytes(), q2.tobytes())
        self.assertEqual(k1.tobytes(), k2.tobytes())
        self.assertEqual(v1.tobytes(), v2.tobytes())


if __name__ == "__main__":
    unittest.main()
