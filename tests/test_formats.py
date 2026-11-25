from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from ragdiag.canonical import checksum_of
from ragdiag.errors import (
    E_FORMAT_CORRUPT,
    E_VERSION_UNSUPPORTED,
    WorkbenchError,
)
from ragdiag.formats import (
    load_repository,
    parse_tool_result,
    read_repository_bytes,
)
from ragdiag.runtime import run_workbench
from ragdiag.sample_generator import generate


class FormatReaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory()
        generate(Path(cls.temp.name) / "data")
        cls.repository = load_repository(
            Path(cls.temp.name) / "data" / "tool_results.json"
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp.cleanup()

    def test_three_formats_and_byte_determinism(self) -> None:
        versions = {}
        for result_ref in ("res/weather/001", "res/weather/002", "res/weather/003"):
            parsed = parse_tool_result(
                self.repository.entries[result_ref],
                self.repository.snapshot_version,
            )
            versions[parsed["format_version"]] = parsed
        self.assertEqual(set(versions), {"v1", "v2", "v3"})
        self.assertEqual(versions["v1"]["numeric"]["temperature"], 18.5)
        self.assertEqual(versions["v2"]["numeric"]["humidity"], 58.0)
        self.assertEqual(
            versions["v3"]["numeric"]["metrics.temperature"], 20.25
        )
        first = read_repository_bytes(self.repository)
        second = read_repository_bytes(self.repository)
        self.assertEqual(first, second)
        self.assertTrue(first.startswith(b"["))

    def test_unknown_version(self) -> None:
        with self.assertRaises(WorkbenchError) as caught:
            parse_tool_result(
                copy.deepcopy(self.repository.entries["res/weather/001"]),
                self.repository.snapshot_version,
                "v99",
            )
        self.assertEqual(caught.exception.code, E_VERSION_UNSUPPORTED)

    def test_metadata_only_version_dispatch(self) -> None:
        v1 = copy.deepcopy(self.repository.entries["res/weather/001"])
        v1["content"] = v1["content"].removeprefix("v1|")
        v1["checksum"] = checksum_of(v1["content"])
        parsed_v1 = parse_tool_result(v1, self.repository.snapshot_version)
        self.assertEqual(parsed_v1["format_version"], "v1")
        self.assertEqual(parsed_v1["numeric"]["temperature"], 18.5)

        v2 = copy.deepcopy(self.repository.entries["res/weather/002"])
        v2["content"] = "\n".join(
            line
            for line in v2["content"].splitlines()
            if not line.startswith("format_version=")
        )
        v2["fields"] = [
            field for field in v2["fields"] if field != "format_version"
        ]
        v2["field_count"] = len(v2["fields"])
        v2["checksum"] = checksum_of(v2["content"])
        parsed_v2 = parse_tool_result(v2, self.repository.snapshot_version)
        self.assertEqual(parsed_v2["format_version"], "v2")
        self.assertEqual(parsed_v2["numeric"]["humidity"], 58.0)
        v2_semicolon = copy.deepcopy(v2)
        v2_semicolon["content"] = v2_semicolon["content"].replace("\n", ";")
        v2_semicolon["checksum"] = checksum_of(v2_semicolon["content"])
        parsed_semicolon = parse_tool_result(
            v2_semicolon, self.repository.snapshot_version
        )
        self.assertEqual(parsed_semicolon["fields"]["city"], "Shanghai")
        v2_json = copy.deepcopy(v2)
        v2_json["content"] = json.dumps(
            {
                "kind": "weather",
                "city": "Shanghai",
                "temperature": 19.0,
                "humidity": 58,
                "condition": "cloudy",
            },
            sort_keys=True,
        )
        v2_json["fields"] = [
            "kind",
            "city",
            "temperature",
            "humidity",
            "condition",
        ]
        v2_json["field_count"] = 5
        v2_json["checksum"] = checksum_of(v2_json["content"])
        parsed_json = parse_tool_result(
            v2_json, self.repository.snapshot_version
        )
        self.assertEqual(parsed_json["fields"]["condition"], "cloudy")

        v3 = copy.deepcopy(self.repository.entries["res/weather/003"])
        v3["content"] = copy.deepcopy(v3["content"])
        v3["content"].pop("format_version")
        v3["checksum"] = checksum_of(v3["content"])
        parsed_v3 = parse_tool_result(v3, self.repository.snapshot_version)
        self.assertEqual(parsed_v3["format_version"], "v3")
        self.assertEqual(
            parsed_v3["numeric"]["metrics.temperature"], 20.25
        )

    def test_v1_field_count_corruption(self) -> None:
        entry = copy.deepcopy(self.repository.entries["res/weather/001"])
        entry["content"] = "v1|weather|Shanghai|18.5"
        entry["checksum"] = checksum_of(entry["content"])
        with self.assertRaises(WorkbenchError) as caught:
            parse_tool_result(entry, self.repository.snapshot_version)
        self.assertEqual(caught.exception.code, E_FORMAT_CORRUPT)
        self.assertEqual(caught.exception.details["expected"], 6)

    def test_v2_missing_key_corruption(self) -> None:
        entry = copy.deepcopy(self.repository.entries["res/weather/002"])
        entry["content"] = "\n".join(
            line
            for line in entry["content"].splitlines()
            if not line.startswith("humidity=")
        )
        entry["checksum"] = checksum_of(entry["content"])
        with self.assertRaises(WorkbenchError) as caught:
            parse_tool_result(entry, self.repository.snapshot_version)
        self.assertEqual(caught.exception.code, E_FORMAT_CORRUPT)
        self.assertEqual(caught.exception.details["key"], "humidity")

    def test_v3_numeric_corruption(self) -> None:
        entry = copy.deepcopy(self.repository.entries["res/weather/003"])
        entry["content"] = copy.deepcopy(entry["content"])
        entry["content"]["metrics"]["humidity"] = "not-a-number"
        entry["checksum"] = checksum_of(entry["content"])
        with self.assertRaises(WorkbenchError) as caught:
            parse_tool_result(entry, self.repository.snapshot_version)
        self.assertEqual(caught.exception.code, E_FORMAT_CORRUPT)
        self.assertEqual(
            caught.exception.details["field"], "metrics.humidity"
        )

    def test_checksum_corruption(self) -> None:
        entry = copy.deepcopy(self.repository.entries["res/weather/002"])
        entry["content"] = entry["content"].replace(
            "humidity=58", "humidity=99"
        )
        with self.assertRaises(WorkbenchError) as caught:
            parse_tool_result(entry, self.repository.snapshot_version)
        self.assertEqual(caught.exception.code, E_FORMAT_CORRUPT)
        self.assertIn("checksum", caught.exception.message)

    def test_corrupt_entry_is_scoped_to_request_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            generate(data)
            repository = json.loads(
                (data / "tool_results.json").read_text(encoding="utf-8")
            )
            for entry in repository["entries"]:
                if entry["result_ref"] == "res/weather/001":
                    entry["content"] = "v1|weather|Shanghai|18.5"
                    entry["checksum"] = checksum_of(entry["content"])
            bad_path = data / "tool_results.corrupt.json"
            bad_path.write_text(
                json.dumps(repository, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )
            report = run_workbench(
                bad_path,
                data / "requests.jsonl",
                data / "schedule.json",
                data / "evidence_corpus.jsonl",
                root / "cache",
            )
            codes = {
                request["unit_id"]: request["business_code"]
                for request in report["requests"]
            }
            self.assertEqual(codes["conv-001@1"], E_FORMAT_CORRUPT)
            self.assertEqual(codes["conv-008@1"], E_FORMAT_CORRUPT)
            self.assertGreaterEqual(
                report["summary"]["business_codes"].get(E_FORMAT_CORRUPT, 0),
                2,
            )


if __name__ == "__main__":
    unittest.main()

