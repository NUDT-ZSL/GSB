from __future__ import annotations

import unittest
from pathlib import Path

import numpy as np

from latentflow.config import VideoConfig
from latentflow.decoder import (
    DeterministicDecoder,
    assemble_blocks,
    compare_frame_sequences,
    verify_anchor_frame,
)
from latentflow.errors import BusinessError, ErrorCode
from latentflow.imaging import make_reference_frame
from latentflow.pipeline import generate_latents
from latentflow.planner import DecodePlanner


def small_config(**overrides: object) -> VideoConfig:
    values: dict[str, object] = {
        "prompt": "deterministic local ridge",
        "width": 48,
        "height": 32,
        "frames": 7,
        "steps": 3,
        "seed": 424242,
        "block_size": 3,
        "latency_budget_ms": 1000.0,
    }
    values.update(overrides)
    return VideoConfig(**values)


class DeterministicPipelineTests(unittest.TestCase):
    def test_same_inputs_produce_identical_latent_bytes_and_hashes(self) -> None:
        first = generate_latents(small_config())
        second = generate_latents(small_config())

        self.assertEqual(first.latents.tobytes(), second.latents.tobytes())
        self.assertEqual(first.initial_latent.tobytes(), second.initial_latent.tobytes())
        self.assertEqual(first.condition.tobytes(), second.condition.tobytes())
        self.assertEqual(first.stage_hashes, second.stage_hashes)
        self.assertEqual(first.prompt_hash, second.prompt_hash)
        self.assertEqual(first.latents_sha256, second.latents_sha256)

    def test_seed_and_prompt_change_latents(self) -> None:
        baseline = generate_latents(small_config())
        other_seed = generate_latents(small_config(seed=424243))
        other_prompt = generate_latents(small_config(prompt="another deterministic prompt"))

        self.assertNotEqual(baseline.initial_latent.tobytes(), other_seed.initial_latent.tobytes())
        self.assertNotEqual(baseline.prompt_hash, other_prompt.prompt_hash)
        self.assertNotEqual(baseline.latents_sha256, other_prompt.latents_sha256)


class ChunkConsistencyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = small_config()
        cls.generation = generate_latents(cls.config)
        cls.anchor = make_reference_frame(cls.config.width, cls.config.height)

    def test_multiple_block_sizes_match_one_shot_decode_byte_for_byte(self) -> None:
        reference_decoder = DeterministicDecoder(
            self.config, self.generation.latents, self.anchor
        )
        whole = reference_decoder.decode_all()
        self.assertTrue(np.array_equal(whole.frames[0], self.anchor))

        for block_size in (1, 2, 3, 5, 7):
            with self.subTest(block_size=block_size):
                decoder = DeterministicDecoder(
                    self.config, self.generation.latents, self.anchor
                )
                blocks = decoder.decode_chunks(block_size)
                assembled = assemble_blocks(blocks, self.config.frames)
                report = compare_frame_sequences(
                    assembled, whole.frames, block_size
                )
                self.assertTrue(report["equal"])
                self.assertEqual(report["sha256"], whole.sha256)
                self.assertTrue(np.array_equal(assembled[0], self.anchor))

    def test_mismatch_reports_first_block_frame_and_pixel(self) -> None:
        decoder = DeterministicDecoder(
            self.config, self.generation.latents, self.anchor
        )
        whole = decoder.decode_all()
        blocks = decoder.decode_chunks(2)
        assembled = assemble_blocks(blocks, self.config.frames)
        frame_index, y, x, channel = 5, 9, 11, 2
        assembled[frame_index, y, x, channel] ^= np.uint8(0x01)

        with self.assertRaises(BusinessError) as captured:
            compare_frame_sequences(assembled, whole.frames, 2)
        error = captured.exception
        self.assertEqual(error.code, ErrorCode.E_CHUNK_MISMATCH)
        self.assertEqual(error.details["block_index"], 2)
        self.assertEqual(error.details["frame_index"], frame_index)
        self.assertEqual(
            error.details["pixel_position"],
            {"x": x, "y": y, "channel": channel},
        )

    def test_anchor_verification_reports_anchor_mismatch(self) -> None:
        decoder = DeterministicDecoder(
            self.config, self.generation.latents, self.anchor
        )
        decoded = decoder.decode_all().frames.copy()
        decoded[0, 4, 6, 1] ^= np.uint8(1)

        with self.assertRaises(BusinessError) as captured:
            verify_anchor_frame(decoded, self.anchor)
        error = captured.exception
        self.assertEqual(error.code, ErrorCode.E_ANCHOR_MISMATCH)
        self.assertEqual(error.details["frame_index"], 0)
        self.assertEqual(error.details["pixel_position"], {"x": 6, "y": 4, "channel": 1})


class PlannerTests(unittest.TestCase):
    def test_planner_selects_requested_block_and_records_actual_estimate(self) -> None:
        config = small_config(block_size=3, latency_budget_ms=1000.0)
        generation = generate_latents(config)
        decoder = DeterministicDecoder(config, generation.latents)
        plan = DecodePlanner(config, decoder).plan(3)

        self.assertEqual(plan.block_size, 3)
        self.assertGreater(plan.estimated_first_block_ms, 0.0)
        self.assertEqual(plan.estimate_basis["method"], "local_decode_loop_probe")
        self.assertEqual(plan.estimate_basis["estimated_frames"], 3)
        self.assertEqual(plan.estimate_basis["decode_stages"], 4)
        self.assertTrue(plan.consistency_policy["byte_equality_is_hard_invariant"])

    def test_impossible_latency_budget_returns_structured_error(self) -> None:
        config = small_config(block_size=7, latency_budget_ms=0.0001)
        generation = generate_latents(config)
        decoder = DeterministicDecoder(config, generation.latents)

        with self.assertRaises(BusinessError) as captured:
            DecodePlanner(config, decoder).plan()
        error = captured.exception
        self.assertEqual(error.code, ErrorCode.E_LATENCY_BUDGET)
        self.assertEqual(error.details["stage"], "decode_planning")
        self.assertEqual(error.details["decoded_blocks"], 0)
        self.assertIn("completed_stages", error.details)
        self.assertIn("rejected_candidates", error.details)

    def test_chunk_memory_limit_degrades_block_size(self) -> None:
        frame_bytes = 48 * 32 * 3
        config = small_config(
            block_size=7,
            latency_budget_ms=1000.0,
            max_chunk_bytes=frame_bytes * 2,
        )
        generation = generate_latents(config)
        decoder = DeterministicDecoder(config, generation.latents)
        plan = DecodePlanner(config, decoder).plan()

        self.assertEqual(plan.block_size, 2)
        self.assertIn("block_size_reduced_for_memory", plan.degradations)
        self.assertLessEqual(plan.chunk_memory_bytes, config.max_chunk_bytes)

    def test_invalid_anchor_strength_returns_config_error(self) -> None:
        with self.assertRaises(BusinessError) as captured:
            VideoConfig.from_dict(
                {"anchor_strength": 1.25, "anchor_path": "samples/reference.ppm"}
            )
        self.assertEqual(captured.exception.code, ErrorCode.E_CONFIG_INVALID)
        self.assertEqual(captured.exception.details["field"], "anchor_strength")

    def test_frame_count_limit_returns_resource_error(self) -> None:
        with self.assertRaises(BusinessError) as captured:
            VideoConfig.from_dict({"frames": 11, "max_frames": 10})
        self.assertEqual(captured.exception.code, ErrorCode.E_RESOURCE_LIMIT)
        self.assertEqual(captured.exception.details["resource"], "frames")


if __name__ == "__main__":
    unittest.main()
