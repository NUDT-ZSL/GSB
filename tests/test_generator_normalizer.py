import unittest

import bootstrap as _bootstrap  # 导入即把 src 加入 sys.path
_SRC_PATH = _bootstrap.SRC
import numpy as np

from piano_tempo.generator import generate_dataset, midi_to_name
from piano_tempo.normalizer import load_record, midi_to_name as norm_midi_to_name
from piano_tempo.normalizer import name_to_midi


class PitchTests(unittest.TestCase):
    def test_name_midi_roundtrip(self):
        for midi in [36, 60, 61, 72, 83, 48]:
            self.assertEqual(name_to_midi(midi_to_name(midi)), midi)

    def test_flat_alias(self):
        self.assertEqual(name_to_midi("Bb3"), name_to_midi("A#3"))


class NormalizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.files = generate_dataset(
            seed=7, n_students=1, pieces_per_student=1, output_dir="artifacts/test_data"
        )

    def test_three_versions_normalize_identical(self):
        records = {}
        for path in self.files:
            record = load_record(path, grid_step_beats=1.0)
            records[record.format_version] = record

        self.assertEqual(set(records), {"v1", "v2", "v3"})
        baseline = records["v1"]
        for version in ("v2", "v3"):
            other = records[version]
            self.assertEqual(
                [event.pitch for event in baseline.events],
                [event.pitch for event in other.events],
            )
            self.assertEqual(
                [event.voice for event in baseline.events],
                [event.voice for event in other.events],
            )
            base_onsets = np.asarray(
                [event.onset_beat for event in baseline.events]
            )
            other_onsets = np.asarray(
                [event.onset_beat for event in other.events]
            )
            np.testing.assert_allclose(
                base_onsets, other_onsets, rtol=1e-6, atol=1e-6
            )
            np.testing.assert_allclose(
                baseline.reference_curve["tempo_bpm"],
                other.reference_curve["tempo_bpm"],
                rtol=0.02,
                atol=1.5,
            )
            np.testing.assert_array_equal(
                baseline.reference_curve["grid_beats"],
                other.reference_curve["grid_beats"],
            )

    def test_voice_and_duration_preserved(self):
        record = load_record(self.files[0], grid_step_beats=1.0)
        voices = {event.voice for event in record.events}
        self.assertEqual(voices, {"rh", "lh"})
        self.assertTrue(all(event.duration_beats > 0 for event in record.events))
        self.assertTrue(all(event.pitch_name == norm_midi_to_name(event.pitch)
                            for event in record.events))


if __name__ == "__main__":
    unittest.main()
