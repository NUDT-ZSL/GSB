from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from timbre_lab.errors import DomainError, E_FRAME_MISMATCH
from timbre_lab.features import extract_and_validate, extract_features
from timbre_lab.synthesis import (
    generate_dataset,
    load_dataset,
    utterance_frame_count_from_seed,
)


class SynthesisAndFeatureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.dataset = generate_dataset(
            cls.root / "a",
            seed=12345,
            num_speakers=2,
            utterances_per_speaker=1,
        )
        cls.reloaded = load_dataset(cls.root / "a")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp.cleanup()

    def test_generation_is_byte_identical_for_same_seed(self) -> None:
        second = generate_dataset(
            self.root / "b",
            seed=12345,
            num_speakers=2,
            utterances_per_speaker=1,
        )
        first_files = sorted(
            path.relative_to(self.dataset.root)
            for path in self.dataset.root.rglob("*")
            if path.is_file()
        )
        second_files = sorted(
            path.relative_to(second.root)
            for path in second.root.rglob("*")
            if path.is_file()
        )
        self.assertEqual(first_files, second_files)
        for relative in first_files:
            self.assertEqual(
                (self.dataset.root / relative).read_bytes(),
                (second.root / relative).read_bytes(),
                msg=str(relative),
            )

    def test_manifest_and_truth_contract(self) -> None:
        required = {
            "utterance_id",
            "speaker_id",
            "ref_id",
            "sample_rate",
            "frame_ms",
            "pos_scale",
            "precision_tier",
            "seed",
        }
        for entry in self.dataset.manifest_entries:
            self.assertTrue(required.issubset(entry))
            self.assertEqual(entry["sample_rate"], 16000)
            self.assertEqual(entry["frame_ms"], 20.0)
            self.assertEqual(
                entry["num_frames"],
                utterance_frame_count_from_seed(entry["seed"]),
            )
        for utterance in self.dataset.utterances:
            self.assertEqual(len(utterance.truth), utterance.frame_count)
            self.assertEqual(
                [frame.frame_index for frame in utterance.truth],
                list(range(utterance.frame_count)),
            )
            self.assertTrue(all(frame.phoneme_id in range(8) for frame in utterance.truth))

    def test_pcm_reload_matches_in_memory_samples(self) -> None:
        self.assertEqual(len(self.reloaded.utterances), len(self.dataset.utterances))
        by_id = {item.utterance_id: item for item in self.reloaded.utterances}
        for original in self.dataset.utterances:
            loaded = by_id[original.utterance_id]
            self.assertEqual(loaded.pcm.tobytes(), original.pcm.tobytes())
            self.assertEqual(loaded.truth, original.truth)

    def test_feature_serialization_is_byte_identical(self) -> None:
        utterance = self.dataset.utterances[0]
        first = extract_and_validate(
            utterance.pcm,
            utterance.truth,
            utterance_id=utterance.utterance_id,
        )
        second = extract_and_validate(
            utterance.pcm,
            utterance.truth,
            utterance_id=utterance.utterance_id,
        )
        self.assertEqual(first.to_jsonl_bytes(), second.to_jsonl_bytes())
        self.assertEqual(first.digest(), second.digest())
        self.assertEqual(first.frame_count, len(utterance.truth))

    def test_exact_frame_boundaries(self) -> None:
        utterance = self.dataset.utterances[0]
        features = extract_features(utterance.pcm, utterance_id=utterance.utterance_id)
        for index in range(features.frame_count):
            self.assertEqual(int(features.start_samples[index]), index * 320)
            self.assertEqual(int(features.end_samples[index]), index * 320 + 320)

    def test_frame_mismatch_reports_first_index(self) -> None:
        utterance = self.dataset.utterances[0]
        broken_truth = utterance.truth[:-2]
        with self.assertRaises(DomainError) as context:
            extract_and_validate(
                utterance.pcm,
                broken_truth,
                utterance_id=utterance.utterance_id,
            )
        error = context.exception
        self.assertEqual(error.code, E_FRAME_MISMATCH)
        self.assertEqual(error.stage, "feature_frames")
        self.assertEqual(error.utterance_id, utterance.utterance_id)
        self.assertEqual(
            error.details["first_mismatch_frame"],
            len(broken_truth),
        )


if __name__ == "__main__":
    unittest.main()

