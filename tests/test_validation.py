from __future__ import annotations

import shutil
import unittest
from dataclasses import replace

import numpy as np

from spkvc.config import PipelineConfig
from spkvc.dataset import Dataset, read_wav, validate_entries, write_wav
from spkvc.errors import BusinessError
from spkvc.models import ManifestEntry

from .support import DatasetTestCase


class ValidationTests(DatasetTestCase):
    def test_invalid_sample_rate_reports_field(self):
        with self.assertRaises(BusinessError) as caught:
            PipelineConfig(sample_rate=8000).validate()
        self.assertEqual(caught.exception.code, "E_INVALID_SAMPLE_RATE")
        self.assertEqual(caught.exception.field, "sample_rate")

    def test_audio_sample_rate_mismatch_reports_field(self):
        path = self.tmp / "eight-khz.wav"
        write_wav(path, np.zeros(800, dtype=np.int16), 8000)
        with self.assertRaises(BusinessError) as caught:
            read_wav(path, expected_sample_rate=16000)
        self.assertEqual(caught.exception.code, "E_INVALID_SAMPLE_RATE")
        self.assertEqual(caught.exception.field, "sample_rate")

    def test_invalid_frame_length_reports_field(self):
        with self.assertRaises(BusinessError) as caught:
            PipelineConfig(frame_ms=17).validate()
        self.assertEqual(caught.exception.code, "E_INVALID_FRAME_MS")
        self.assertEqual(caught.exception.field, "frame_ms")

    def test_hop_larger_than_frame_reports_field(self):
        with self.assertRaises(BusinessError) as caught:
            PipelineConfig(frame_ms=20, hop_ms=30).validate()
        self.assertEqual(caught.exception.code, "E_INVALID_FRAME_HOP")
        self.assertEqual(caught.exception.field, "hop_ms")

    def test_duplicate_utterance_id(self):
        entries = self.dataset.load_entries()
        with self.assertRaises(BusinessError) as caught:
            validate_entries(entries + [entries[0]])
        self.assertEqual(caught.exception.code, "E_DUPLICATE_UTTERANCE_ID")
        self.assertEqual(caught.exception.field, "utterance_id")

    def test_missing_ref_id_key_or_value(self):
        entry = self.dataset.load_entries()[0].to_dict()
        del entry["ref_id"]
        with self.assertRaises(BusinessError) as caught:
            ManifestEntry.from_dict(entry)
        self.assertEqual(caught.exception.code, "E_MISSING_REF_ID")
        self.assertEqual(caught.exception.field, "ref_id")

        empty = self.dataset.load_entries()[0]
        empty = replace(empty, ref_id="")
        with self.assertRaises(BusinessError) as caught:
            validate_entries([empty])
        self.assertEqual(caught.exception.code, "E_MISSING_REF_ID")

    def test_unknown_precision_profile(self):
        entry = replace(
            self.dataset.load_entries()[0], precision_profile="unknown-profile"
        )
        with self.assertRaises(BusinessError) as caught:
            validate_entries([entry])
        self.assertEqual(caught.exception.code, "E_UNKNOWN_PRECISION_PROFILE")
        self.assertEqual(caught.exception.field, "precision_profile")

    def test_truth_frame_count_mismatch_returns_structured_error(self):
        dataset_root = self.tmp / "frame-mismatch-data"
        shutil.copytree(self.dataset_root, dataset_root)
        dataset = Dataset(dataset_root)
        entry = sorted(dataset.load_entries(), key=lambda item: item.utterance_id)[0]
        truth_path = dataset.truth_path(entry.utterance_id)
        lines = truth_path.read_text(encoding="utf-8").splitlines()
        truth_path.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
        engine = self.engine("frame-mismatch-state")
        engine.dataset = dataset
        with self.assertRaises(BusinessError) as caught:
            engine.run()
        error = caught.exception
        self.assertEqual(error.code, "E_FRAME_MISMATCH")
        self.assertEqual(error.details["utterance_id"], entry.utterance_id)
        self.assertEqual(
            error.details["first_deviation_frame"], len(lines) - 1
        )
        self.assertEqual(error.details["truth_frame_count"], len(lines) - 1)
        self.assertEqual(len(engine.snapshots.load().entries), 4)


if __name__ == "__main__":
    unittest.main()

