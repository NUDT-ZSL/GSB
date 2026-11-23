from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from meeting_retrieval.errors import (
    BusinessError,
    E_DUPLICATE_PAIR_ID,
    E_GRID_INVALID,
    E_IMAGE_SHAPE_MISMATCH,
    E_TEXT_EMPTY,
    E_VOCAB_MISSING,
    E_VOCAB_OUT_OF_RANGE,
)
from meeting_retrieval.service import MeetingRetrievalApp
from meeting_retrieval.text import out_of_range_token


class ValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.app = MeetingRetrievalApp(Path(self.temp.name) / "ws")
        self.valid = self.app.load_samples()[0].to_dict()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_duplicate_pair_id(self) -> None:
        with self.assertRaises(BusinessError) as raised:
            self.app.validate_sample_payloads([self.valid, dict(self.valid)])
        self.assertEqual(raised.exception.code, E_DUPLICATE_PAIR_ID)
        self.assertEqual(raised.exception.pair_id, "p01")
        self.assertEqual(raised.exception.field, "pair_id")
        self.assertEqual(raised.exception.details["first_line"], 1)
        self.assertEqual(raised.exception.details["duplicate_line"], 2)

    def test_canvas_shape_mismatch(self) -> None:
        payload = dict(self.valid)
        payload["image_shape"] = [100, 100, 3]
        with self.assertRaises(BusinessError) as raised:
            self.app.validate_sample_payloads([payload])
        self.assertEqual(raised.exception.code, E_IMAGE_SHAPE_MISMATCH)
        self.assertEqual(raised.exception.pair_id, "p01")
        self.assertEqual(raised.exception.field, "image_shape")

    def test_empty_text(self) -> None:
        payload = dict(self.valid)
        payload["text"] = "   "
        with self.assertRaises(BusinessError) as raised:
            self.app.validate_sample_payloads([payload])
        self.assertEqual(raised.exception.code, E_TEXT_EMPTY)
        self.assertEqual(raised.exception.pair_id, "p01")
        self.assertEqual(raised.exception.field, "text")

    def test_non_positive_grid(self) -> None:
        with self.assertRaises(BusinessError) as raised:
            self.app.set_feature_params(tile_rows=0)
        self.assertEqual(raised.exception.code, E_GRID_INVALID)
        self.assertEqual(raised.exception.field, "tile_grid")

    def test_missing_vocab_term(self) -> None:
        with self.assertRaises(BusinessError) as raised:
            self.app.update_text("p01", "完全不在词表中的词")
        self.assertEqual(raised.exception.code, E_VOCAB_MISSING)
        self.assertEqual(raised.exception.pair_id, "p01")
        self.assertEqual(raised.exception.field, "text")
        self.assertIn("token", raised.exception.details)
        self.assertEqual(self.app.load_samples()[0].text, self.valid["text"])
        self.assertIsNone(self.app.recover()["run_id"])

    def test_vocab_index_out_of_range(self) -> None:
        with self.assertRaises(BusinessError) as raised:
            out_of_range_token(self.app.load_vocabulary(), 10_000, "p01")
        self.assertEqual(raised.exception.code, E_VOCAB_OUT_OF_RANGE)
        self.assertEqual(raised.exception.pair_id, "p01")
        self.assertEqual(raised.exception.field, "token_index")

    def test_last_error_is_persisted_for_ui(self) -> None:
        with self.assertRaises(BusinessError):
            self.app.update_text("p01", "完全不在词表中的词")
        restarted = MeetingRetrievalApp(Path(self.temp.name) / "ws")
        self.assertEqual(restarted.last_error["code"], E_VOCAB_MISSING)
        self.assertEqual(restarted.status()["last_error"]["pair_id"], "p01")


if __name__ == "__main__":
    unittest.main()
