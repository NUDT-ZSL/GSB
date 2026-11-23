from __future__ import annotations

import unittest

import numpy as np

from rope_arbiter.models import CaseSpec, ResolvedCase, VariantSnapshot
from rope_arbiter.sample import build_sample_cases
from rope_arbiter.variants import (
    MappingRegistry,
    adjudicate_angles,
    build_builtin_mapping_registry,
    compute_angles,
    generate_qkv,
    rotate_qk,
)


def resolved(case: CaseSpec) -> ResolvedCase:
    snapshot = VariantSnapshot.from_cases([case], 1)
    return ResolvedCase.resolve(case, snapshot)


class VariantTests(unittest.TestCase):
    def setUp(self) -> None:
        self.case = build_sample_cases()[0]
        self.resolved = resolved(self.case)

    def test_rope_angles_are_deterministic_half_split(self) -> None:
        first = compute_angles(self.resolved, "rope")
        second = compute_angles(self.resolved, "rope")
        np.testing.assert_array_equal(first, second)
        self.assertEqual(first.shape, (self.case.seq_len, self.case.head_dim // 2))
        np.testing.assert_allclose(first[:, 0], np.arange(self.case.seq_len))

    def test_position_interpolation_mapping_ratio(self) -> None:
        rope = compute_angles(self.resolved, "rope")
        interpolated = compute_angles(self.resolved, "position_interpolation")
        np.testing.assert_allclose(
            interpolated, rope / self.case.scale_ratio, rtol=0.0, atol=1e-15
        )

    def test_ntk_and_block_transforms_are_finite_and_declared(self) -> None:
        registry = build_builtin_mapping_registry()
        for target in ("position_interpolation", "ntk_aware", "block_frequency"):
            angles = compute_angles(self.resolved, target)
            self.assertTrue(np.all(np.isfinite(angles)))
            self.assertIsNotNone(registry.get("rope", target))

    def test_qkv_and_rotation_are_byte_deterministic(self) -> None:
        first_qkv = generate_qkv(self.resolved)
        second_qkv = generate_qkv(self.resolved)
        for name in ("q", "k", "v"):
            self.assertEqual(first_qkv[name].tobytes(), second_qkv[name].tobytes())
        angles = compute_angles(self.resolved, "rope")
        q1, k1 = rotate_qk(first_qkv["q"], first_qkv["k"], angles)
        q2, k2 = rotate_qk(second_qkv["q"], second_qkv["k"], angles)
        self.assertEqual(q1.tobytes(), q2.tobytes())
        self.assertEqual(k1.tobytes(), k2.tobytes())
        pairs = self.case.head_dim // 2
        q_pairs = first_qkv["q"]
        rotated_pairs = q1
        np.testing.assert_allclose(
            np.sqrt(
                rotated_pairs[..., :pairs] ** 2
                + rotated_pairs[..., pairs:] ** 2
            ),
            np.sqrt(
                q_pairs[..., :pairs] ** 2 + q_pairs[..., pairs:] ** 2
            ),
        )

    def test_mapping_undeclared_and_drift_codes(self) -> None:
        rope = compute_angles(self.resolved, "rope")
        ntk = compute_angles(self.resolved, "ntk_aware")
        undeclared = adjudicate_angles(
            self.resolved,
            "rope",
            "ntk_aware",
            rope,
            ntk,
            MappingRegistry(),
            tolerance=1e-9,
        )
        self.assertEqual(undeclared.code, "E_MAPPING_UNDECLARED")
        self.assertEqual(undeclared.first_violation["dimension_index"], 0)

        drifted_ntk = ntk.copy()
        drifted_ntk[0, 0] += 0.25
        drift = adjudicate_angles(
            self.resolved,
            "rope",
            "ntk_aware",
            rope,
            drifted_ntk,
            build_builtin_mapping_registry(),
            tolerance=1e-9,
        )
        self.assertEqual(drift.code, "E_MAPPING_DRIFT")
        self.assertEqual(drift.first_violation["position"], 0)
        self.assertEqual(drift.first_violation["dimension_index"], 0)
        self.assertAlmostEqual(drift.first_violation["deviation"], 0.25)
        self.assertGreater(drift.max_residual, 0.0)


if __name__ == "__main__":
    unittest.main()
