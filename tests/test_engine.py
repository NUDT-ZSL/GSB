from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np

from flash_attn_cache import (
    FlashAttentionEngine,
    QKVArrays,
    RuntimeConfig,
    dense_attention_reference,
)

from tests.helpers import sample


class EngineTests(unittest.TestCase):
    def test_dense_reference_and_self_check(self) -> None:
        for case_id in ("basic_causal", "dense_float32", "partial_tail"):
            with self.subTest(case_id=case_id), tempfile.TemporaryDirectory() as td:
                case, arrays = sample(case_id)
                engine = FlashAttentionEngine(Path(td))
                result = engine.compute(case, arrays)
                self.assertTrue(result.ok, result.errors)
                reference = dense_attention_reference(case, arrays)
                np.testing.assert_allclose(
                    result.output,
                    reference,
                    rtol=1e-10,
                    atol=1e-10,
                )
                check = engine.self_check(case, arrays, result)
                self.assertTrue(check.ok, [issue.to_dict() for issue in check.issues])

    def test_determinism_across_parallelism(self) -> None:
        case, arrays = sample("basic_causal")
        with tempfile.TemporaryDirectory() as left, tempfile.TemporaryDirectory() as right:
            e1 = FlashAttentionEngine(
                Path(left),
                runtime=RuntimeConfig(parallel_nodes=1, max_parallel_nodes=1),
            )
            e2 = FlashAttentionEngine(
                Path(right),
                runtime=RuntimeConfig(parallel_nodes=4, max_parallel_nodes=4),
            )
            r1 = e1.compute(case, arrays)
            r2 = e2.compute(case, arrays)
            self.assertTrue(r1.ok)
            self.assertTrue(r2.ok)
            self.assertEqual(r1.output.tobytes(), r2.output.tobytes())
            self.assertEqual(set(r1.node_outcomes), set(r2.node_outcomes))
            for node_id in r1.node_outcomes:
                self.assertEqual(
                    r1.node_outcomes[node_id].content_hash,
                    r2.node_outcomes[node_id].content_hash,
                    node_id,
                )
                left_arrays = r1.node_outcomes[node_id].arrays
                right_arrays = r2.node_outcomes[node_id].arrays
                self.assertEqual(set(left_arrays), set(right_arrays))
                for name in left_arrays:
                    self.assertEqual(
                        left_arrays[name].tobytes(),
                        right_arrays[name].tobytes(),
                        f"{node_id}:{name}",
                    )

    def test_second_call_reuses_every_node(self) -> None:
        case, arrays = sample("basic_causal")
        with tempfile.TemporaryDirectory() as td:
            engine = FlashAttentionEngine(Path(td))
            first = engine.compute(case, arrays)
            second = engine.compute(case, arrays)
            self.assertTrue(second.ok)
            self.assertEqual(second.recomputed_nodes, ())
            self.assertEqual(len(second.reused_nodes), len(second.node_outcomes))
            self.assertEqual(first.output.tobytes(), second.output.tobytes())
            for outcome in second.node_outcomes.values():
                self.assertTrue(outcome.cache_hit)

    def test_cache_keys_track_semantics_content_and_revision_lineage(self) -> None:
        case, arrays = sample("basic_causal")
        with tempfile.TemporaryDirectory() as td:
            engine = FlashAttentionEngine(Path(td))
            causal = engine.compute(case, arrays)
            non_causal = engine.compute(case.replace(causal=False), arrays)
            self.assertNotEqual(
                causal.node_outcomes["score:0000:0000"].cache_key,
                non_causal.node_outcomes["score:0000:0000"].cache_key,
            )

            changed_k = arrays.k.copy()
            changed_k[:, 0:4, :] += 0.3
            changed_arrays = QKVArrays(arrays.q, changed_k, arrays.v)
            content_changed = engine.compute(case, changed_arrays)
            self.assertNotEqual(
                causal.node_outcomes["split_kv:0000"].cache_key,
                content_changed.node_outcomes["split_kv:0000"].cache_key,
            )
            self.assertNotEqual(
                causal.node_outcomes["score:0000:0000"].cache_key,
                content_changed.node_outcomes["score:0000:0000"].cache_key,
            )

            revision_changed = engine.compute(
                case.replace(kv_revision=(1, 0, 0)),
                arrays,
            )
            self.assertEqual(
                causal.node_outcomes["split_kv:0000"].content_hash,
                revision_changed.node_outcomes["split_kv:0000"].content_hash,
            )
            self.assertNotEqual(
                causal.node_outcomes["score:0000:0000"].cache_key,
                revision_changed.node_outcomes["score:0000:0000"].cache_key,
            )

    def test_revision_only_invalidates_downstream_of_kvload(self) -> None:
        case, arrays = sample("basic_causal")
        with tempfile.TemporaryDirectory() as td:
            engine = FlashAttentionEngine(Path(td))
            baseline = engine.compute(case, arrays)
            self.assertTrue(baseline.ok)
            updated_case = case.replace(kv_revision=(1, 0, 0))
            updated = engine.incremental_update(
                case,
                arrays,
                updated_case,
                arrays,
                baseline_result=baseline,
            )
            self.assertTrue(updated.ok, updated.attention.errors)
            self.assertEqual(updated.changed_kv_blocks, (0,))
            self.assertIn("split_kv:0000", updated.reused_nodes)
            self.assertIn("score:0000:0000", updated.recomputed_nodes)
            self.assertIn("softmax:0001:0000", updated.recomputed_nodes)
            self.assertIn("accum:0002:0000", updated.recomputed_nodes)
            self.assertFalse(
                set(updated.recomputed_nodes) & set(updated.reused_nodes)
            )
            self.assertTrue(
                set(updated.affected_nodes).issubset(
                    set(updated.recomputed_nodes) | set(updated.reused_nodes)
                )
            )
            self.assertEqual(
                baseline.output.tobytes(),
                updated.attention.output.tobytes(),
            )
            self.assertTrue(updated.self_check.ok, updated.self_check.to_dict())

    def test_incremental_content_replacement_matches_full_recompute(self) -> None:
        case, arrays = sample("basic_causal")
        with tempfile.TemporaryDirectory() as td:
            engine = FlashAttentionEngine(Path(td))
            baseline = engine.compute(case, arrays)
            updated_case = case.replace(kv_revision=(9, 0, 0))
            k = arrays.k.copy()
            v = arrays.v.copy()
            k[:, 0:4, :] += 0.2
            v[:, 0:4, :] -= 0.1
            updated_arrays = QKVArrays(arrays.q.copy(), k, v)
            updated = engine.incremental_update(
                case,
                arrays,
                updated_case,
                updated_arrays,
                baseline_result=baseline,
            )
            self.assertTrue(updated.ok, updated.attention.errors)
            self.assertEqual(updated.changed_kv_blocks, (0,))
            self.assertEqual(
                set(updated.recomputed_nodes),
                set(updated.affected_nodes),
            )
            self.assertTrue(updated.reused_nodes)
            reference = dense_attention_reference(updated_case, updated_arrays)
            np.testing.assert_allclose(
                updated.attention.output,
                reference,
                rtol=1e-10,
                atol=1e-10,
            )
            self.assertTrue(updated.self_check.ok, updated.self_check.to_dict())

    def test_growing_non_causal_kv_recomputes_only_accumulation_suffix(self) -> None:
        case, arrays = sample("dense_float32")
        rng = np.random.default_rng(991)
        q = np.concatenate(
            [
                arrays.q,
                rng.standard_normal((1, 4, 3), dtype=np.float32),
            ],
            axis=1,
        )
        k = np.concatenate(
            [
                arrays.k,
                rng.standard_normal((1, 4, 3), dtype=np.float32),
            ],
            axis=1,
        )
        v = np.concatenate(
            [
                arrays.v,
                rng.standard_normal((1, 4, 3), dtype=np.float32),
            ],
            axis=1,
        )
        updated_case = case.replace(
            seq_len=12,
            kv_revision=(0, 0, 1),
        )
        updated_arrays = QKVArrays(q, k, v)
        with tempfile.TemporaryDirectory() as td:
            engine = FlashAttentionEngine(Path(td))
            baseline = engine.compute(case, arrays)
            updated = engine.incremental_update(
                case,
                arrays,
                updated_case,
                updated_arrays,
                baseline_result=baseline,
            )
            self.assertTrue(updated.ok, updated.attention.errors)
            self.assertEqual(updated.changed_kv_blocks, (2,))
            self.assertEqual(updated.changed_q_blocks, (2,))
            self.assertIn("score:0000:0000", updated.reused_nodes)
            self.assertIn("accum:0000:0000", updated.reused_nodes)
            self.assertIn("score:0000:0002", updated.recomputed_nodes)
            self.assertIn("accum:0000:0002", updated.recomputed_nodes)
            self.assertTrue(np.isfinite(updated.attention.output).all())
            reference = dense_attention_reference(updated_case, updated_arrays)
            np.testing.assert_allclose(
                updated.attention.output,
                reference,
                rtol=1e-9,
                atol=1e-9,
            )
            self.assertTrue(updated.self_check.ok, updated.self_check.to_dict())

    def test_self_check_detects_tampered_output(self) -> None:
        case, arrays = sample("basic_causal")
        with tempfile.TemporaryDirectory() as td:
            engine = FlashAttentionEngine(Path(td))
            result = engine.compute(case, arrays)
            result.output = result.output + 1e-3
            check = engine.self_check(case, arrays, result)
            self.assertFalse(check.ok)
            self.assertTrue(
                any(
                    issue.code == "E_INCONSISTENT"
                    and issue.category == "output_mismatch"
                    for issue in check.issues
                ),
                [issue.to_dict() for issue in check.issues],
            )

    def test_nan_softmax_is_attributed_to_score_node(self) -> None:
        case, _ = sample("basic_causal")
        shape = (case.n_heads, case.seq_len, case.head_dim)
        overflow = np.full(shape, 1e308, dtype=np.float64)
        arrays = QKVArrays(overflow.copy(), overflow.copy(), overflow.copy())
        with tempfile.TemporaryDirectory() as td:
            engine = FlashAttentionEngine(Path(td))
            result = engine.compute(case, arrays)
            self.assertFalse(result.ok)
            matching = [
                issue
                for issue in result.errors
                if issue.code == "E_SOFTMAX_NAN"
                and issue.node_id
                and issue.node_id.startswith("score:")
            ]
            self.assertTrue(
                matching,
                [issue.to_dict() for issue in result.errors],
            )
            self.assertEqual(matching[0].case_id, case.case_id)
            self.assertIsNotNone(matching[0].block_index)


if __name__ == "__main__":
    unittest.main()
