from __future__ import annotations

import unittest

import numpy as np

from vocalforge.alignment import (
    _rank_candidates,
    align_exact,
    alignment_digest,
    validate_truth_phonemes,
)
from vocalforge.errors import AppError
from vocalforge.features import extract_dataset_features
from vocalforge.model import FeatureFrame, FrameTruth
from vocalforge.precision import NumericOps
from vocalforge.synth import synthesize_dataset


class SynthFeatureAlignmentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.dataset = synthesize_dataset()
        cls.features = extract_dataset_features(
            cls.dataset.utterances,
            cls.dataset.frame_ms,
            cls.dataset.frame_hop_ms,
            NumericOps("float64"),
        )
        cls.alignment = align_exact(
            cls.features,
            cls.dataset.controlled_phonemes,
            cls.dataset.phoneme_prototypes,
        )

    def test_dataset_contract(self) -> None:
        self.assertEqual(self.dataset.sample_rate, 16000)
        self.assertEqual(self.dataset.frame_ms, 20.0)
        self.assertEqual(self.dataset.frame_hop_ms, 10.0)
        self.assertEqual(len(self.dataset.speakers), 4)
        self.assertEqual(len(self.dataset.utterances), 12)
        self.assertEqual(self.dataset.total_frames, 460)
        for utterance in self.dataset.utterances:
            self.assertEqual(utterance.pcm.dtype, np.dtype("<i2"))
            self.assertEqual(utterance.sample_rate, 16000)
            self.assertEqual(len(utterance.truth), len(utterance.timeline))
            for frame in utterance.truth:
                self.assertIn(frame.phoneme_id, self.dataset.controlled_phonemes)

    def test_frame_boundaries_are_exact_sample_indices(self) -> None:
        utterance = self.dataset.utterances[0]
        frames = self.features[utterance.utterance_id]
        self.assertEqual(frames[0].start_sample, 0)
        self.assertEqual(frames[0].end_sample, 320)
        self.assertEqual(frames[1].start_sample, 160)
        self.assertEqual(frames[1].end_sample, 480)
        self.assertEqual(len(frames), len(utterance.truth))
        self.assertLessEqual(frames[-1].end_sample, utterance.pcm.size)

    def test_synthesis_and_alignment_digests_are_deterministic(self) -> None:
        dataset_b = synthesize_dataset()
        features_b = extract_dataset_features(
            dataset_b.utterances,
            dataset_b.frame_ms,
            dataset_b.frame_hop_ms,
            NumericOps("float64"),
        )
        alignment_b = align_exact(
            features_b,
            dataset_b.controlled_phonemes,
            dataset_b.phoneme_prototypes,
        )
        self.assertEqual(
            [u.pcm.tobytes() for u in self.dataset.utterances],
            [u.pcm.tobytes() for u in dataset_b.utterances],
        )
        self.assertEqual(
            alignment_digest(self.alignment),
            alignment_digest(alignment_b),
        )
        for utterance_id in self.features:
            self.assertEqual(
                [frame.to_dict() for frame in self.features[utterance_id]],
                [frame.to_dict() for frame in features_b[utterance_id]],
            )

    def test_alignment_labels_stay_in_controlled_set(self) -> None:
        controlled = set(self.dataset.controlled_phonemes)
        for rows in self.alignment.values():
            for row in rows:
                self.assertIn(row.phoneme_id, controlled)

    def test_out_of_range_truth_includes_utterance_and_frame(self) -> None:
        truth = {
            utterance.utterance_id: utterance.truth
            for utterance in self.dataset.utterances
        }
        first = self.dataset.utterances[0]
        changed = list(first.truth)
        changed[4] = FrameTruth(
            frame_index=4,
            speaker_id=first.speaker_id,
            f0_true=changed[4].f0_true,
            energy_true=changed[4].energy_true,
            phoneme_id=99,
        )
        truth[first.utterance_id] = tuple(changed)
        with self.assertRaises(AppError) as context:
            validate_truth_phonemes(truth, self.dataset.controlled_phonemes)
        error = context.exception
        self.assertEqual(error.code, "E_PHONEME_OUT_OF_RANGE")
        self.assertEqual(error.details["utterance_id"], first.utterance_id)
        self.assertEqual(error.details["frame_index"], 4)

    def test_tie_break_uses_speaker_then_phoneme(self) -> None:
        frame = FeatureFrame(
            utterance_id="tie",
            speaker_id=2,
            frame_index=0,
            start_sample=0,
            end_sample=320,
            energy=1.0,
            zcr=0.0,
            f0_candidate=100.0,
            log_energy=1.0,
            centroid_hz=1000.0,
            band_energies=(1.0, 0.0, 0.0, 0.0),
            timbre_vector=(1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0),
        )
        prototypes = {
            phoneme_id: {
                "phoneme_id": phoneme_id,
                "formants_hz": [500.0, 1500.0, 2500.0],
                "amplitude": 1.0,
                "noise": 0.1,
            }
            for phoneme_id in (3, 1, 2)
        }
        ranked = _rank_candidates(frame, (3, 1, 2), prototypes)
        self.assertEqual([item[1] for item in ranked], [1, 2, 3])


if __name__ == "__main__":
    unittest.main()
