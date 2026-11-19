from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np

from flash_attn_cache import (
    BusinessError,
    FlashAttentionEngine,
    RuntimeConfig,
    SoftmaxConfig,
    dense_attention_reference,
)
from flash_attn_cache.compute import protect_norm

from tests.helpers import sample


class ResourceAndStabilityTests(unittest.TestCase):
    def test_memory_limit_returns_resource_limit(self) -> None:
        case, arrays = sample("basic_causal")
        with tempfile.TemporaryDirectory() as td:
            engine = FlashAttentionEngine(
                Path(td),
                runtime=RuntimeConfig(max_resident_bytes=1),
            )
            result = engine.compute(case, arrays)
            self.assertFalse(result.ok)
            self.assertEqual(result.errors[0].code, "E_RESOURCE_LIMIT")
            self.assertEqual(result.errors[0].category, "resident_memory")
            self.assertEqual(result.errors[0].case_id, case.case_id)

    def test_cache_entry_limit_during_execution(self) -> None:
        case, arrays = sample("basic_causal")
        with tempfile.TemporaryDirectory() as td:
            engine = FlashAttentionEngine(
                Path(td),
                runtime=RuntimeConfig(
                    max_cache_entries=1,
                    parallel_nodes=2,
                    max_parallel_nodes=2,
                ),
            )
            result = engine.compute(case, arrays)
            self.assertFalse(result.ok)
            issue = next(
                item for item in result.errors if item.code == "E_RESOURCE_LIMIT"
            )
            self.assertEqual(issue.category, "cache_entries")
            self.assertIsNotNone(issue.node_id)

    def test_parallelism_degradation_is_marked(self) -> None:
        case, arrays = sample("basic_causal")
        with tempfile.TemporaryDirectory() as td:
            engine = FlashAttentionEngine(
                Path(td),
                runtime=RuntimeConfig(
                    parallel_nodes=8,
                    max_parallel_nodes=2,
                ),
            )
            result = engine.compute(case, arrays)
            self.assertTrue(result.ok)
            self.assertTrue(result.degraded)
            self.assertIn("parallelism_limited", result.degradation_categories)

    def test_corrupt_cache_is_reported_and_downstream_recomputed(self) -> None:
        case, arrays = sample("basic_causal")
        with tempfile.TemporaryDirectory() as td:
            engine = FlashAttentionEngine(Path(td))
            baseline = engine.compute(case, arrays)
            self.assertTrue(baseline.ok)
            target = baseline.node_outcomes["score:0000:0000"]
            entry_path = Path(td) / "entries" / f"{target.cache_key}.bin"
            blob = bytearray(entry_path.read_bytes())
            blob[-1] ^= 0x5A
            entry_path.write_bytes(bytes(blob))
            recovered = engine.compute(case, arrays)
            self.assertTrue(recovered.ok, recovered.errors)
            corrupt = [
                issue
                for issue in recovered.errors
                if issue.code == "E_CACHE_CORRUPT"
            ]
            self.assertTrue(corrupt)
            self.assertTrue(corrupt[0].recovered)
            self.assertEqual(corrupt[0].node_id, "score:0000:0000")
            self.assertIn("score:0000:0000", recovered.recomputed_nodes)
            self.assertIn("softmax:0000:0000", recovered.recomputed_nodes)
            self.assertIn("accum:0000:0000", recovered.recomputed_nodes)
            self.assertGreater(
                recovered.node_outcomes["score:0000:0000"].key_version,
                target.key_version,
            )
            np.testing.assert_allclose(
                recovered.output,
                dense_attention_reference(case, arrays),
                rtol=1e-10,
                atol=1e-10,
            )

    def test_norm_zero_and_subnormal_floor(self) -> None:
        config = SoftmaxConfig(subnormal_floor=0.0)
        with self.assertRaises(BusinessError) as context:
            protect_norm(
                np.array([0.0]),
                config,
                node_id="softmax:unit",
                block_index=0,
                case_id="unit",
            )
        self.assertEqual(context.exception.issue.code, "E_NORM_ZERO")
        protected = protect_norm(
            np.array([0.0, 1e-320]),
            SoftmaxConfig(subnormal_floor=1e-300),
            node_id="softmax:unit",
            block_index=0,
            case_id="unit",
        )
        self.assertTrue(np.isfinite(protected).all())
        self.assertTrue((protected >= 1e-300).all())

    def test_rounding_modes_remain_finite_and_deterministic(self) -> None:
        case, arrays = sample("partial_tail")
        for mode in ("nearest", "truncate"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as td:
                config = SoftmaxConfig(
                    subnormal_floor=1e-300,
                    rounding_mode=mode,
                    rounding_precision=40,
                )
                engine = FlashAttentionEngine(Path(td), softmax=config)
                first = engine.compute(case, arrays)
                second = engine.compute(case, arrays)
                self.assertTrue(first.ok, first.errors)
                self.assertTrue(second.ok, second.errors)
                self.assertTrue(np.isfinite(first.output).all())
                self.assertEqual(first.output.tobytes(), second.output.tobytes())

    def test_engine_returns_norm_zero_when_protection_is_disabled(self) -> None:
        from flash_attn_cache import AttentionCase, QKVArrays

        case = AttentionCase(
            case_id="norm_zero",
            seq_len=2,
            head_dim=1,
            n_heads=1,
            block_size=1,
            causal=True,
            dtype="float64",
            seed=1,
            kv_revision=0,
            scale=1.0,
        )
        q = np.full((1, 2, 1), 1000.0)
        k = np.full((1, 2, 1), -1.0)
        v = np.ones((1, 2, 1))
        arrays = QKVArrays(q, k, v)
        config = SoftmaxConfig(
            subnormal_floor=0.0,
            rounding_mode="truncate",
            rounding_precision=0,
        )
        with tempfile.TemporaryDirectory() as td:
            engine = FlashAttentionEngine(Path(td), softmax=config)
            result = engine.compute(case, arrays)
            self.assertFalse(result.ok)
            issue = next(
                item for item in result.errors if item.code == "E_NORM_ZERO"
            )
            self.assertEqual(issue.case_id, case.case_id)
            self.assertIsNotNone(issue.node_id)


if __name__ == "__main__":
    unittest.main()
