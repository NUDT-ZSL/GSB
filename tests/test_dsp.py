from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from spkvc.config import PipelineConfig
from spkvc.dataset import read_wav
from spkvc.dsp import (
    align_frames,
    choose_alignment_candidate,
    estimate_timbre,
    extract_features,
    frame_count_for_samples,
)
from spkvc.models import canonical_bytes
from spkvc.models import sha256_bytes
from spkvc.pipeline import ConversionEngine

from .support import DatasetTestCase


class DspTests(DatasetTestCase):
    def test_feature_extraction_is_byte_stable_and_frame_count_is_independent(self):
        entry = self.dataset.load_entries()[0]
        config = PipelineConfig()
        pcm, raw_hash = read_wav(
            self.dataset.utterance_audio_path(entry.utterance_id)
        )
        raw_hash = sha256_bytes(raw_hash)
        first = extract_features(
            entry=entry, pcm=pcm, raw_pcm_hash=raw_hash, config=config
        )
        second = extract_features(
            entry=entry, pcm=pcm, raw_pcm_hash=raw_hash, config=config
        )
        self.assertEqual(canonical_bytes(first), canonical_bytes(second))
        expected = frame_count_for_samples(
            len(pcm), config.frame_samples, config.hop_samples
        )
        self.assertEqual(first["frame_count"], expected)
        self.assertEqual(
            first["frame_count"],
            1 + (len(pcm) - config.frame_samples) // config.hop_samples,
        )
        self.assertEqual(len(first["frames"]), first["frame_count"])
        for key in ("energy_rms", "zcr", "f0_candidate"):
            self.assertIn(key, first["frames"][0])

    def test_timbre_and_alignment_do_not_open_truth_files(self):
        entry = self.dataset.load_entries()[0]
        config = PipelineConfig()
        pcm, raw_hash = read_wav(
            self.dataset.utterance_audio_path(entry.utterance_id)
        )
        raw_hash = sha256_bytes(raw_hash)
        ref_pcm, ref_hash = read_wav(
            self.dataset.ref_audio_path(entry.ref_id, entry.ref_revision)
        )
        ref_hash = sha256_bytes(ref_hash)
        with mock.patch("builtins.open", side_effect=AssertionError("truth file opened")):
            features = extract_features(
                entry=entry, pcm=pcm, raw_pcm_hash=raw_hash, config=config
            )
            timbre = estimate_timbre(
                ref_id=entry.ref_id,
                ref_speaker_id="spk-test",
                ref_revision=entry.ref_revision,
                ref_pcm=ref_pcm,
                ref_pcm_hash=ref_hash,
                config=config,
                precision_profile=entry.precision_profile,
            )
            alignment = align_frames(
                features=features, timbre=timbre, config=config
            )
        self.assertEqual(timbre["normalization"], "l2")
        self.assertEqual(len(timbre["embedding"]), timbre["dimension"])
        self.assertEqual(alignment["frame_count"], features["frame_count"])

    def test_alignment_tie_break_is_speaker_then_phoneme(self):
        candidates = [
            {"speaker_id": "spk-2", "phoneme_id": 0, "score": 0.5},
            {"speaker_id": "spk-1", "phoneme_id": 3, "score": 0.5},
            {"speaker_id": "spk-1", "phoneme_id": 1, "score": 0.5},
            {"speaker_id": "spk-0", "phoneme_id": 9, "score": 0.4},
        ]
        chosen = choose_alignment_candidate(candidates)
        self.assertEqual(
            (chosen["speaker_id"], chosen["phoneme_id"]), ("spk-1", 1)
        )
        higher = choose_alignment_candidate(candidates + [
            {"speaker_id": "spk-9", "phoneme_id": 9, "score": 0.8}
        ])
        self.assertEqual(higher["score"], 0.8)

    def test_same_seed_and_config_reproduce_dataset_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            from spkvc.dataset import generate_dataset

            other = generate_dataset(Path(tmp) / "data", force=True)
            original_manifest = self.dataset.manifest_path.read_bytes()
            regenerated_manifest = other.manifest_path.read_bytes()
            self.assertEqual(original_manifest, regenerated_manifest)
            entry = self.dataset.load_entries()[0]
            self.assertEqual(
                self.dataset.utterance_audio_path(entry.utterance_id).read_bytes(),
                other.utterance_audio_path(entry.utterance_id).read_bytes(),
            )

    def test_overlapping_hop_runs_end_to_end_with_truth_alignment(self):
        with tempfile.TemporaryDirectory() as tmp:
            from spkvc.dataset import generate_dataset

            dataset = generate_dataset(
                Path(tmp) / "overlap",
                force=True,
                frame_ms=20,
                hop_ms=10,
            )
            engine = ConversionEngine(
                dataset=dataset,
                state_dir=Path(tmp) / "state",
                output_dir=Path(tmp) / "outputs",
            )
            result = engine.run()
            self.assertTrue(engine.self_check(result)["ok"])
            entry = dataset.load_entries()[0]
            truth = dataset.read_truth(entry.utterance_id)
            self.assertGreater(len(truth), 1)


if __name__ == "__main__":
    unittest.main()

