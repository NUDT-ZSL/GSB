import unittest
from dataclasses import replace

from mmpipe.config import PipelineConfig
from mmpipe.errors import (
    DupPairError,
    HintInvalidError,
    MaskInvalidError,
    PairIdInvalidError,
    SeedShapeError,
    TextEmptyError,
)
from mmpipe.manifest import PairRecord, canonical_order, generate_manifest, load_manifest, validate_manifest
from mmpipe.weights import expected_image_shape


def base_record(pair_id="p1", **overrides):
    config = PipelineConfig()
    seed = overrides.get("image_seed", 555)
    shape = expected_image_shape(seed, config.block_size)
    defaults = dict(
        pair_id=pair_id,
        image_seed=seed,
        image_shape=shape,
        text_tokens=("tok00", "tok01"),
        modality_mask=3,
        stage_hint="steady",
        priority=0,
    )
    defaults.update(overrides)
    return PairRecord(**defaults)


class ManifestValidationTests(unittest.TestCase):
    def setUp(self):
        self.config = PipelineConfig()

    def test_valid_manifest_passes(self):
        records = [base_record("a"), base_record("b", image_seed=556)]
        self.assertEqual(len(validate_manifest(records, self.config)), 2)

    def test_duplicate_pair_id(self):
        records = [base_record("dup"), base_record("dup", image_seed=557)]
        with self.assertRaises(DupPairError) as ctx:
            validate_manifest(records, self.config)
        self.assertEqual(ctx.exception.code, "E_DUP_PAIR")
        self.assertEqual(ctx.exception.pair_id, "dup")

    def test_invalid_mask(self):
        with self.assertRaises(MaskInvalidError) as ctx:
            validate_manifest([base_record(modality_mask=0)], self.config)
        self.assertEqual(ctx.exception.code, "E_MASK_INVALID")

    def test_seed_shape_mismatch(self):
        record = base_record()
        broken = replace(record, image_shape=(16, 16, 3))
        with self.assertRaises(SeedShapeError) as ctx:
            validate_manifest([broken], self.config)
        self.assertEqual(ctx.exception.code, "E_SEED_SHAPE")

    def test_empty_text(self):
        from mmpipe.stages import TEXT_STAGE, text_encode
        from mmpipe.weights import FixedWeights

        record = base_record(text_tokens=())
        with self.assertRaises(TextEmptyError) as ctx:
            text_encode(record, self.config, FixedWeights(self.config))
        self.assertEqual(ctx.exception.code, "E_TEXT_EMPTY")
        self.assertEqual(ctx.exception.stage, TEXT_STAGE)
        self.assertEqual(ctx.exception.pair_id, "p1")

    def test_unsafe_pair_id_and_bad_hint(self):
        with self.assertRaises(PairIdInvalidError):
            validate_manifest([base_record(pair_id="bad/id")], self.config)
        with self.assertRaises(HintInvalidError) as ctx:
            validate_manifest([base_record(stage_hint="nope")], self.config)
        self.assertEqual(ctx.exception.code, "E_HINT_INVALID")

    def test_generated_manifest_roundtrip_and_order(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            records = generate_manifest(tmp, self.config, num_pairs=5)
            loaded = load_manifest(f"{tmp}/manifest.jsonl")
            self.assertEqual([r.pair_id for r in records], [r.pair_id for r in loaded])
            validate_manifest(loaded, self.config)
            ordered = canonical_order(loaded)
            self.assertEqual(ordered, sorted(loaded, key=lambda r: (-r.priority, r.pair_id)))


if __name__ == "__main__":
    unittest.main()

