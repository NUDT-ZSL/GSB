from __future__ import annotations

import dataclasses
import unittest

import numpy as np

from rope_arbiter.attention import (
    compare_variant_scores,
    compute_block_scores,
    compute_online_normalization,
)
from rope_arbiter.models import ResolvedCase, VariantSnapshot
from rope_arbiter.sample import build_sample_cases
from rope_arbiter.variants import compute_angles, generate_qkv, rotate_qk


class AttentionTests(unittest.TestCase):
    def _inputs(self, causal: bool, block_size: int = 3):
        case = dataclasses.replace(build_sample_cases()[0], causal=causal, block_size=block_size)
        resolved = ResolvedCase.resolve(case, VariantSnapshot.from_cases([case], 1))
        qkv = generate_qkv(resolved)
        angles = compute_angles(resolved, "rope")
        q_rotated, k_rotated = rotate_qk(qkv["q"], qkv["k"], angles)
        blocks = compute_block_scores(
            q_rotated, k_rotated, block_size, causal, tolerance=1e-9
        )
        return qkv, blocks

    def test_block_and_full_paths_match_for_both_causal_modes(self) -> None:
        for causal in (True, False):
            qkv, blocks = self._inputs(causal)
            self.assertEqual(blocks.code, "OK")
            self.assertLessEqual(blocks.block_score_max_abs_error, 1e-9)
            normalization = compute_online_normalization(
                blocks, qkv["v"], causal, tolerance=1e-9
            )
            self.assertEqual(normalization.code, "OK")
            self.assertLessEqual(normalization.attention_output_max_abs_error, 1e-9)
            self.assertLessEqual(normalization.normalization_max_abs_error, 1e-9)
            self.assertIsNone(normalization.first_mismatch)

    def test_ranking_consistency_is_one_for_identical_scores(self) -> None:
        _, blocks = self._inputs(True)
        comparison = compare_variant_scores(
            blocks.scores, blocks.scores.copy(), True, tolerance=1e-9
        )
        self.assertEqual(comparison["max_abs_deviation"], 0.0)
        self.assertEqual(comparison["ranking_consistency_ratio"], 1.0)

    def test_block_mismatch_reports_first_element(self) -> None:
        qkv, blocks = self._inputs(True)
        changed_blocks = blocks.blocks.copy()
        changed_blocks[0, 0, 0, 1, 0] += 0.5
        changed = dataclasses.replace(blocks, blocks=changed_blocks)
        normalization = compute_online_normalization(
            changed, qkv["v"], True, tolerance=1e-9
        )
        self.assertEqual(normalization.code, "E_BLOCK_MISMATCH")
        self.assertIsNotNone(normalization.first_mismatch)
        self.assertEqual(normalization.first_mismatch["kind"], "attention_output_vs_full")
        self.assertEqual(normalization.first_mismatch["element_index"][0], 0)
        self.assertEqual(normalization.first_mismatch["element_index"][1], 1)


if __name__ == "__main__":
    unittest.main()
