"""业务要求 1/6：RoPE 确定性、半维配对、分块在线 softmax 与整段对照。"""

import unittest

import numpy as np

from rope_attn import attention as attn
from rope_attn import rope as rope_mod
from rope_attn import sampling
from rope_attn.numeric import quantize
from tests._helpers import TempWorkspace, make_task


class RopeDeterminismTest(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace = TempWorkspace()

    def tearDown(self) -> None:
        self.workspace.cleanup()

    def test_angles_byte_identical_across_calls(self):
        task = make_task()
        spec = rope_mod.RopeSpec.from_task(task)
        first = rope_mod.compute_angles(spec)
        second = rope_mod.compute_angles(spec)
        self.assertEqual(first.shape, (task.seq_len, task.head_dim // 2))
        self.assertTrue(np.array_equal(first, second))
        self.assertEqual(first.tobytes(), second.tobytes())

    def test_half_dim_pairing_declared(self):
        task = make_task(head_dim=8)
        spec = rope_mod.RopeSpec.from_task(task)
        angles = rope_mod.compute_angles(spec)
        x = sampling.generate_q(task, 0)
        rotated = rope_mod.apply_rotation(x, angles)
        half = 4
        cos = np.cos(angles)[:, None, :]
        sin = np.sin(angles)[:, None, :]
        expect_left = x[..., :half] * cos - x[..., half:] * sin
        np.testing.assert_allclose(rotated[..., :half], expect_left, atol=1e-6)

    def test_variants_produce_distinct_angles(self):
        base = make_task()
        linear = make_task(pos_encoding_variant="linear", pos_scale=0.5)
        ntk = make_task(pos_encoding_variant="ntk", pos_scale=2.0)
        angles_std = rope_mod.compute_angles(rope_mod.RopeSpec.from_task(base))
        angles_linear = rope_mod.compute_angles(
            rope_mod.RopeSpec.from_task(linear)
        )
        angles_ntk = rope_mod.compute_angles(rope_mod.RopeSpec.from_task(ntk))
        self.assertFalse(np.allclose(angles_std, angles_linear))
        self.assertFalse(np.allclose(angles_std, angles_ntk))

    def test_generation_byte_identical(self):
        task = make_task()
        q1 = sampling.generate_q(task, 0)
        q2 = sampling.generate_q(task, 0)
        self.assertEqual(q1.tobytes(), q2.tobytes())
        k1, v1 = sampling.generate_kv(task, 0, 1)
        k2, v2 = sampling.generate_kv(task, 0, 1)
        self.assertEqual(k1.tobytes(), k2.tobytes())
        self.assertEqual(v1.tobytes(), v2.tobytes())


class ChunkedAttentionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace = TempWorkspace()

    def tearDown(self) -> None:
        self.workspace.cleanup()

    def test_chunked_matches_full_reference_causal(self):
        task = make_task(causal=True)
        config = self.workspace.config()
        q = sampling.generate_q(task, 0)
        k, v = sampling.generate_kv(task, 0, 1)
        spec = rope_mod.RopeSpec.from_task(task)
        angles = rope_mod.compute_angles(spec)
        rq = rope_mod.apply_rotation(q, angles)
        rk = rope_mod.apply_rotation(k, angles)

        state = attn.init_online_state((task.seq_len, task.num_heads),
                                       task.head_dim)
        block_stats = []
        for block_index, (start, end) in enumerate(
            attn.block_ranges(task.seq_len, task.block_size)
        ):
            scores = attn.scaled_dot_product(rq, rk[start:end])
            scores = attn.apply_causal_mask(scores, 0, start)
            stats = attn.compute_block_stats(
                task, rq, rk, block_index, (0, task.seq_len),
                (start, end), config
            )
            block_stats.append(stats)
            state = attn.update_online_state(state, stats, v, scores)
        chunked = attn.finalize_output(state, task.dtype)
        reference, _ref_max, ref_denom = attn.full_reference(
            task, rq, rk, v
        )
        np.testing.assert_allclose(chunked, reference, atol=1e-6)

        restored_max, restored = attn.merged_full_denom(block_stats)
        error = attn.denominator_restore_error(restored, ref_denom)
        self.assertLess(float(np.max(error)), config.tolerance)

    def test_causal_blocks_future_positions(self):
        task = make_task(causal=True, seq_len=6, block_size=2)
        q = sampling.generate_q(task, 0)
        k, _v = sampling.generate_kv(task, 0, 1)
        scores = attn.scaled_dot_product(q, k)
        masked = attn.apply_causal_mask(scores, 0, 0)
        for query in range(task.seq_len):
            for key_pos in range(task.seq_len):
                if key_pos > query:
                    self.assertTrue(np.isneginf(masked[query, key_pos, :]).all())

    def test_rounding_modes_and_subnormal_flush(self):
        # 选取在 float32 网格上存在真实舍入歧义的小数。
        value = np.array(
            [0.123456789012345, -0.123456789012345, 1.0, 2.0],
            dtype=np.float64,
        )
        half_even = quantize(value, "float32", "half_to_even", False)
        half_up = quantize(value, "float32", "half_up", False)
        truncate = quantize(value, "float32", "truncate", False)
        down = quantize(value, "float32", "down", False)
        # 可表示整数在所有模式下保持不变。
        self.assertEqual(float(np.asarray(half_even)[2]), 1.0)
        self.assertEqual(float(np.asarray(truncate)[2]), 1.0)
        # 正数 truncate/down 不大于原生最近邻，负数 truncate 不小于最近邻。
        self.assertLessEqual(
            float(np.asarray(truncate)[0]), float(np.asarray(half_even)[0])
        )
        self.assertLessEqual(
            float(np.asarray(down)[0]), float(np.asarray(half_even)[0])
        )
        self.assertGreaterEqual(
            float(np.asarray(truncate)[1]), float(np.asarray(half_even)[1])
        )
        # half_up 取远离零一侧：正数不小于最近邻，负数不大于最近邻。
        self.assertGreaterEqual(
            float(np.asarray(half_up)[0]), float(np.asarray(half_even)[0])
        )
        # 负小数 half_up 朝更负方向走，故数值上 <= 最近邻。
        self.assertLessEqual(
            float(np.asarray(half_up)[1]), float(np.asarray(half_even)[1])
        )
        tiny = np.array([5e-40], dtype=np.float32)
        flushed = quantize(tiny, "float32", "half_to_even", True)
        self.assertEqual(float(np.asarray(flushed)[0]), 0.0)


if __name__ == "__main__":
    unittest.main()
