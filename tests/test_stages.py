import unittest

import numpy as np

from mmpipe.config import PipelineConfig
from mmpipe.errors import MaskInvalidError
from mmpipe.manifest import PairRecord
from mmpipe.stages import fuse, image_encode, score, text_encode
from mmpipe.weights import FixedWeights, generate_image


def make_record(mask=3, tokens=("tok00", "tok01", "tok01"), seed=12345):
    from mmpipe.weights import expected_image_shape

    shape = expected_image_shape(seed, PipelineConfig().block_size)
    return PairRecord(
        pair_id="p1",
        image_seed=seed,
        image_shape=shape,
        text_tokens=tokens,
        modality_mask=mask,
        stage_hint="steady",
        priority=1,
    )


class StageDeterminismTests(unittest.TestCase):
    def setUp(self):
        self.config = PipelineConfig()
        self.weights_a = FixedWeights(self.config)
        self.weights_b = FixedWeights(self.config)

    def test_fixed_weights_byte_identical(self):
        for name in ("image_proj", "text_embed", "gate", "score_ref"):
            a = getattr(self.weights_a, name)
            b = getattr(self.weights_b, name)
            self.assertEqual(a.tobytes(), b.tobytes())
            self.assertEqual(a.dtype, np.float32)

    def test_image_encoding_byte_identical(self):
        record = make_record()
        pixels = generate_image(record.image_seed, record.image_shape)
        first = image_encode(pixels, self.config, self.weights_a)
        second = image_encode(pixels, self.config, self.weights_b)
        self.assertEqual(first.dtype, np.float32)
        self.assertEqual(first.tobytes(), second.tobytes())
        self.assertEqual(first.shape, (self.config.image_dim,))

    def test_text_encoding_frequency_weighted(self):
        record = make_record(tokens=("tok00", "tok01", "tok01"))
        first = text_encode(record, self.config, self.weights_a)
        second = text_encode(record, self.config, self.weights_b)
        self.assertEqual(first.tobytes(), second.tobytes())
        expected = (
            np.float32(1 / 3) * self.weights_a.text_embed[0]
            + np.float32(2 / 3) * self.weights_a.text_embed[1]
        ).astype(np.float32)
        np.testing.assert_array_equal(first, np.ascontiguousarray(expected))

    def test_fusion_mask_semantics(self):
        record = make_record(mask=3)
        pixels = generate_image(record.image_seed, record.image_shape)
        image_emb = image_encode(pixels, self.config, self.weights_a)
        text_emb = text_encode(record, self.config, self.weights_a)

        image_only = fuse(make_record(mask=1), image_emb, text_emb, self.config, self.weights_a)
        text_only = fuse(make_record(mask=2), image_emb, text_emb, self.config, self.weights_a)
        both = fuse(record, image_emb, text_emb, self.config, self.weights_a)

        np.testing.assert_array_equal(image_only[: self.config.image_dim], image_emb)
        np.testing.assert_array_equal(image_only[self.config.image_dim :], np.zeros(self.config.text_dim, np.float32))
        np.testing.assert_array_equal(text_only[: self.config.image_dim], np.zeros(self.config.image_dim, np.float32))
        np.testing.assert_array_equal(text_only[self.config.image_dim :], text_emb)
        self.assertEqual(both.shape, (self.config.fusion_dim,))
        self.assertEqual(both.dtype, np.float32)

    def test_invalid_mask_raises_business_code(self):
        record = make_record(mask=9)
        with self.assertRaises(MaskInvalidError) as ctx:
            fuse(record, np.zeros(8, np.float32), np.zeros(8, np.float32), self.config, self.weights_a)
        self.assertEqual(ctx.exception.code, "E_MASK_INVALID")

    def test_score_is_float32_scalar(self):
        fused = np.linspace(0, 1, self.config.fusion_dim, dtype=np.float32)
        value = score(fused, self.config, self.weights_a)
        self.assertEqual(value.shape, ())
        self.assertEqual(value.dtype, np.float32)
        # Recomputing yields identical bytes.
        self.assertEqual(value.tobytes(), score(fused, self.config, self.weights_b).tobytes())


if __name__ == "__main__":
    unittest.main()

