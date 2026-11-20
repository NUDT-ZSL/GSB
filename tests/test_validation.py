from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tracker_app.data_gen import generate_dataset, make_error_fixtures
from tracker_app.errors import TrackingError
from tracker_app.jsonio import read_jsonl
from tracker_app.validation import preprocess_dataset

from tests.helpers import small_config


class ValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = small_config()
        self.dataset = generate_dataset(self.config)

    def test_generation_is_deterministic(self) -> None:
        second = generate_dataset(self.config)
        self.assertEqual(
            json.dumps(self.dataset, sort_keys=True, separators=(",", ":")),
            json.dumps(second, sort_keys=True, separators=(",", ":")),
        )

    def test_valid_dataset_preprocesses(self) -> None:
        preprocessed = preprocess_dataset(
            self.dataset["detections"],
            self.dataset["truth"],
            self.config,
        )
        self.assertEqual(preprocessed.seq_ids, ["seq-000", "seq-001"])
        self.assertEqual(len(preprocessed.sequences["seq-000"].frames), 24)

    def test_error_fixtures_have_expected_codes_and_locations(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixtures = make_error_fixtures(self.dataset, Path(temp))
            for name, fixture in fixtures.items():
                with self.subTest(name=name):
                    with self.assertRaises(TrackingError) as raised:
                        preprocess_dataset(
                            read_jsonl(fixture["path"]),
                            self.dataset["truth"],
                            self.config,
                        )
                    error = raised.exception
                    self.assertEqual(error.code, fixture["expected_error"])
                    self.assertIsNotNone(error.seq_id)
                    if name in {"bad_bbox", "bad_occluded"}:
                        self.assertEqual(error.frame_index, fixture["frame_index"])
                    if name == "bad_frame_gap":
                        self.assertEqual(error.category, "jump")


if __name__ == "__main__":
    unittest.main()
