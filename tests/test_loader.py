from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from sre_burnrate import SLIValidationError, evaluate_file, load_sli
from sre_burnrate.config import EvaluationConfig

from tests.helpers import make_config, minute_iso, write_ndjson


class LoaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = make_config()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _path(self, name: str) -> Path:
        return self.root / name

    def _assert_sli_error(self, records: list[dict[str, object]], line: int) -> None:
        path = write_ndjson(self._path("invalid.ndjson"), records)
        with self.assertRaises(SLIValidationError) as context:
            load_sli(path, self.config.sli)
        self.assertEqual(context.exception.code, "E_SLI")
        self.assertEqual(context.exception.line_no, line)
        self.assertEqual(context.exception.details["line"], line)
        self.assertEqual(context.exception.to_dict()["line"], line)

    def test_valid_ndjson_and_metadata(self) -> None:
        path = write_ndjson(
            self._path("valid.ndjson"),
            [
                {"minute": minute_iso(0), "requests": 10, "errors": 0},
                {"minute": minute_iso(1), "requests": 11, "errors": 1},
            ],
        )
        loaded = load_sli(path, self.config.sli)
        self.assertEqual(loaded.metadata.row_count, 2)
        self.assertEqual(loaded.metadata.first_minute, 1767225600)
        self.assertEqual(loaded.metadata.last_minute, 1767225660)
        self.assertEqual(len(loaded.metadata.data_sha256), 64)
        self.assertEqual(len(loaded.events), 2)

    def test_duplicate_minute_is_rejected_with_line(self) -> None:
        self._assert_sli_error(
            [
                {"minute": minute_iso(0), "requests": 10, "errors": 0},
                {"minute": minute_iso(0), "requests": 10, "errors": 0},
            ],
            2,
        )

    def test_decreasing_timestamp_is_rejected_with_line(self) -> None:
        self._assert_sli_error(
            [
                {"minute": minute_iso(2), "requests": 10, "errors": 0},
                {"minute": minute_iso(1), "requests": 10, "errors": 0},
            ],
            2,
        )

    def test_failed_greater_than_total_is_rejected(self) -> None:
        self._assert_sli_error(
            [
                {"minute": minute_iso(0), "requests": 10, "errors": 0},
                {"minute": minute_iso(1), "requests": 10, "errors": 11},
            ],
            2,
        )

    def test_boolean_and_float_counts_are_rejected(self) -> None:
        self._assert_sli_error(
            [
                {"minute": minute_iso(0), "requests": 10, "errors": 0},
                {"minute": minute_iso(1), "requests": True, "errors": 0},
            ],
            2,
        )
        self._assert_sli_error(
            [
                {"minute": minute_iso(0), "requests": 10, "errors": 0},
                {"minute": minute_iso(1), "requests": 10.5, "errors": 0},
            ],
            2,
        )

    def test_unaligned_timestamp_is_rejected(self) -> None:
        self._assert_sli_error(
            [
                {"minute": minute_iso(0), "requests": 10, "errors": 0},
                {"minute": "2026-01-01T00:01:30Z", "requests": 10, "errors": 0},
            ],
            2,
        )

    def test_malformed_json_ndjson_is_rejected_with_line(self) -> None:
        path = self._path("malformed.ndjson")
        path.write_text(
            json.dumps({"minute": minute_iso(0), "requests": 10, "errors": 0}) + "\n{broken\n",
            encoding="utf-8",
        )
        with self.assertRaises(SLIValidationError) as context:
            load_sli(path, self.config.sli)
        self.assertEqual(context.exception.line_no, 2)

    def test_json_array_stream_and_line_number(self) -> None:
        path = self._path("records.json")
        path.write_text(
            "[\n"
            '  {"minute":"2026-01-01T00:00:00Z","requests":10,"errors":0},\n'
            '  {"minute":"2026-01-01T00:00:00Z","requests":9,"errors":0}\n'
            "]\n",
            encoding="utf-8",
        )
        with self.assertRaises(SLIValidationError) as context:
            load_sli(path, self.config.sli)
        self.assertEqual(context.exception.line_no, 3)

    def test_json_object_wrapper(self) -> None:
        path = self._path("wrapped.json")
        payload = {
            "metadata": {"owner": "sre"},
            "records": [
                {"minute": minute_iso(0), "requests": 10, "errors": 1},
                {"minute": minute_iso(1), "requests": 20, "errors": 2},
            ],
        }
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        loaded = load_sli(path, self.config.sli)
        self.assertEqual([event.total_requests for event in loaded.events], [10, 20])
        self.assertEqual(loaded.metadata.input_format, "json_document")

    def test_streaming_engine_never_returns_partial_state_on_late_error(self) -> None:
        path = write_ndjson(
            self._path("late_error.ndjson"),
            [
                {"minute": minute_iso(0), "requests": 10, "errors": 0},
                {"minute": minute_iso(1), "requests": 10, "errors": 0},
                {"minute": minute_iso(2), "requests": 10, "errors": 11},
            ],
        )
        streaming_config = make_config(adaptive_threshold=0)
        with self.assertRaises(SLIValidationError) as context:
            evaluate_file(path, streaming_config)
        self.assertEqual(context.exception.code, "E_SLI")
        self.assertEqual(context.exception.line_no, 3)

    def test_config_target_ratio_is_exact(self) -> None:
        payload = make_config().to_dict()
        del payload["slo"]["target_ppm"]
        payload["slo"]["target_ratio"] = "0.999"
        config = EvaluationConfig.from_dict(payload)
        self.assertEqual(config.slo.target_ppm, 999000)


if __name__ == "__main__":
    unittest.main()
