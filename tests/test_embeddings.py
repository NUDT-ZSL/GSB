from __future__ import annotations

import unittest

from vocalforge.embedding import (
    ReferenceRegistry,
    compute_embedding_drift,
    replaced_frame_guard,
)
from vocalforge.errors import AppError
from vocalforge.synth import synthesize_dataset, synthesize_registration_clip


class ReferenceEmbeddingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.dataset = synthesize_dataset()

    def setUp(self) -> None:
        self.registry = ReferenceRegistry(self.dataset)

    def test_append_reestimates_only_changed_speaker(self) -> None:
        old = self.registry.all_summaries()
        old_bytes = {speaker: summary.summary_bytes for speaker, summary in old.items()}
        clip = synthesize_registration_clip(1, "ref_spk_01_v2", 2, self.dataset.seed)
        result = self.registry.append_clip(
            clip, expected_seed=clip.clone_seed, expected_shape=clip.shape
        )
        self.assertEqual(result.changed_speakers, (1,))
        self.assertEqual(result.unchanged_speakers, (2, 3, 4))
        for speaker in (2, 3, 4):
            self.assertEqual(
                old_bytes[speaker],
                self.registry.all_summaries()[speaker].summary_bytes,
            )
        self.assertNotEqual(
            old_bytes[1],
            self.registry.all_summaries()[1].summary_bytes,
        )
        self.assertGreater(result.drifts[0]["drift_normalized_l2"], 0)
        guard = replaced_frame_guard(result, tuple(range(self.dataset.total_frames)))
        self.assertTrue(guard["disjoint"])
        self.assertTrue(guard["covers_all"])

    def test_drift_is_independently_recomputable(self) -> None:
        old = self.registry.all_summaries()[1]
        clip = synthesize_registration_clip(1, "ref_spk_01_v2", 2, self.dataset.seed)
        result = self.registry.append_clip(clip)
        new = self.registry.all_summaries()[1]
        recomputed = compute_embedding_drift(
            old.vector,
            old.normalized,
            new.vector,
            new.normalized,
            old.reference_versions,
            new.reference_versions,
        )
        self.assertEqual(
            result.drifts[0]["recompute_digest"],
            recomputed["recompute_digest"],
        )
        self.assertEqual(
            result.drifts[0]["drift_normalized_l2"],
            recomputed["drift_normalized_l2"],
        )

    def test_same_content_hash_is_idempotent(self) -> None:
        clip = synthesize_registration_clip(1, "ref_spk_01_v2", 2, self.dataset.seed)
        first = self.registry.append_clip(clip)
        before = {
            speaker: summary.summary_bytes
            for speaker, summary in self.registry.all_summaries().items()
        }
        second = self.registry.append_clip(clip)
        after = {
            speaker: summary.summary_bytes
            for speaker, summary in self.registry.all_summaries().items()
        }
        self.assertEqual(first.changed_speakers, (1,))
        self.assertEqual(second.changed_speakers, ())
        self.assertEqual(before, after)

    def test_same_ref_id_with_new_content_triggers_reestimate(self) -> None:
        clip_v2 = synthesize_registration_clip(
            1, "ref_spk_01_v2", 2, self.dataset.seed + 1
        )
        first = self.registry.append_clip(clip_v2)
        clip_changed = synthesize_registration_clip(
            1, "ref_spk_01_v2", 3, self.dataset.seed + 999
        )
        self.assertNotEqual(clip_v2.content_hash, clip_changed.content_hash)
        second = self.registry.append_clip(clip_changed)
        self.assertEqual(first.changed_speakers, (1,))
        self.assertEqual(second.changed_speakers, (1,))
        refs = self.registry.state.clips[1]
        self.assertEqual(len([ref for ref in refs if ref.ref_id == "ref_spk_01_v2"]), 1)
        self.assertEqual(
            [ref for ref in refs if ref.ref_id == "ref_spk_01_v2"][0].content_hash,
            clip_changed.content_hash,
        )

    def test_missing_ref_id_is_structured(self) -> None:
        clip = synthesize_registration_clip(1, "", 2, self.dataset.seed)
        with self.assertRaises(AppError) as context:
            self.registry.append_clip(clip)
        self.assertEqual(context.exception.code, "E_REF_MISSING")
        self.assertEqual(context.exception.field, "ref_id")

    def test_seed_and_shape_mismatch_are_structured(self) -> None:
        clip = synthesize_registration_clip(1, "ref_spk_01_v2", 2, self.dataset.seed)
        with self.assertRaises(AppError) as context:
            self.registry.append_clip(clip, expected_seed=clip.clone_seed + 1)
        self.assertEqual(context.exception.code, "E_SEED_SHAPE")
        self.assertEqual(context.exception.field, "clone_seed")
        with self.assertRaises(AppError) as context:
            self.registry.append_clip(clip, expected_shape=(1,))
        self.assertEqual(context.exception.code, "E_SEED_SHAPE")
        self.assertEqual(context.exception.field, "shape")


if __name__ == "__main__":
    unittest.main()
