"""需求 2：哈希词袋嵌入、潜变量初始化指纹与逐元素可复现。"""
from __future__ import annotations

import tempfile
import unittest

import numpy as np

from vbench.embedding import HashBagOfWords
from vbench.latents import (build_encoder, initialize_image_latent,
                            initialize_text_latent)
from vbench.hashing import fingerprint_array

from ._harness import fresh_workspace, full_render, collect_frames, collect_raw_latents
from ._harness import assert_frames_equal, assert_latents_equal


class EmbeddingTest(unittest.TestCase):
    def test_embedding_is_normalized_and_deterministic(self) -> None:
        encoder = HashBagOfWords(("a", "b", "c", "d"), embed_dim=16)
        first = encoder.embed("a b c")
        second = encoder.embed("a b c")
        np.testing.assert_array_equal(first, second)
        self.assertAlmostEqual(float(np.linalg.norm(first)), 1.0, places=6)

    def test_oov_is_deterministic_but_distinct(self) -> None:
        encoder = HashBagOfWords(("a", "b"), embed_dim=32)
        in_vocab = encoder.embed("a b")
        oov = encoder.embed("zzz qqq")
        self.assertEqual(oov.tolist(), encoder.embed("zzz qqq").tolist())
        self.assertFalse(np.array_equal(in_vocab, oov))

    def test_empty_prompt_zero_vector(self) -> None:
        encoder = HashBagOfWords(("a",), embed_dim=8)
        np.testing.assert_array_equal(encoder.embed(""), np.zeros(8, np.float32))


class InitDeterminismTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp(prefix="vbench-det-")
        self.ws = fresh_workspace(self.temp_dir, "ws", clip_count=2,
                                  frames=4, denoise_steps=5)
        self.config = self.ws.config()
        self.clips, _ = self.ws.clips(self.config)
        self.clip = self.clips[0]
        self.tier = self.config.tiers[1]

    def test_text_init_carries_fingerprint_and_dependencies(self) -> None:
        encoder = build_encoder(self.config)
        embed = encoder.embed(self.clip.prompt)
        artifact_a = initialize_text_latent(
            self.config, self.clip.clip_id, embed, self.tier)
        artifact_b = initialize_text_latent(
            self.config, self.clip.clip_id, embed, self.tier)
        np.testing.assert_array_equal(artifact_a.latent, artifact_b.latent)
        self.assertEqual(artifact_a.fingerprint,
                         fingerprint_array(artifact_a.latent))
        self.assertEqual(artifact_a.fingerprint, artifact_b.fingerprint)
        self.assertEqual(artifact_a.dependencies["kind"], "text")
        self.assertEqual(artifact_a.latent.shape[0], self.config.frames)

    def test_image_init_uses_image_fingerprint(self) -> None:
        image = self.clip.init_images[self.tier.name]
        image_fp = self.clip.init_image_fingerprints[self.tier.name]
        artifact = initialize_image_latent(
            self.config, self.clip.clip_id, image, image_fp, self.tier)
        self.assertEqual(artifact.dependencies["image_fingerprint"], image_fp)
        again = initialize_image_latent(
            self.config, self.clip.clip_id, image, image_fp, self.tier)
        np.testing.assert_array_equal(artifact.latent, again.latent)

    def test_text_and_image_init_differ(self) -> None:
        encoder = build_encoder(self.config)
        embed = encoder.embed(self.clip.prompt)
        text = initialize_text_latent(
            self.config, self.clip.clip_id, embed, self.tier)
        image = initialize_image_latent(
            self.config, self.clip.clip_id,
            self.clip.init_images[self.tier.name],
            self.clip.init_image_fingerprints[self.tier.name], self.tier)
        self.assertFalse(np.array_equal(text.latent, image.latent))

    def test_full_render_repeatable_elementwise(self) -> None:
        first = full_render(self.ws, "run-a")
        second = full_render(self.ws, "run-b")
        assert_frames_equal(
            self, collect_frames(first["store"].root),
            collect_frames(second["store"].root))
        assert_latents_equal(
            self, collect_raw_latents(first["store"].root),
            collect_raw_latents(second["store"].root))
        self.assertEqual(first["report"]["config_hash"],
                         second["report"]["config_hash"])


if __name__ == "__main__":
    unittest.main()
