from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from vmpool.errors import (
    E_CLOCK_ROLLBACK,
    E_DEPENDENCY_CYCLE,
    E_DEPENDENCY_MISSING,
    E_DIGEST_MISMATCH,
    E_JSON_MALFORMED,
    E_NAME_DUPLICATE,
    E_PENDING_CONFLICT,
    E_SAMPLE_MISSING,
    BusinessError,
)
from vmpool.fixed import (
    apply_safety_margin,
    canonical_json,
    convert_to_fixed,
)
from vmpool.model import load_sample

from .helpers import copy_sample, read_json, write_json


class FixedPointTests(unittest.TestCase):
    def test_floor_conversion_is_repeatable_and_bounded(self) -> None:
        first_value, first = convert_to_fixed(
            Decimal("1.2345"), 1000, "x", "conv:test"
        )
        second_value, second = convert_to_fixed(
            Decimal("1.2345"), 1000, "x", "conv:test"
        )
        self.assertEqual(first_value, 1234)
        self.assertEqual(first, second)
        self.assertEqual(first.rounding, "floor")
        self.assertEqual(first.error_upper_bound, "0.5")
        self.assertEqual(first.error_upper_bound_fixed, 1)
        self.assertEqual(first.source_float_repr, "1.2345")
        self.assertEqual(canonical_json(first.as_dict()), canonical_json(second.as_dict()))

    def test_safety_margin_uses_ceiling(self) -> None:
        self.assertEqual(apply_safety_margin(30000, 1000, 10000), 33000)
        self.assertEqual(apply_safety_margin(1, 1, 10000), 2)


class ValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.sample = copy_sample(Path(self.temp.name) / "sample")

    def assert_code(self, expected: str, *, require_manifest: bool = False) -> None:
        with self.assertRaises(BusinessError) as caught:
            load_sample(self.sample, require_manifest=require_manifest)
        self.assertEqual(caught.exception.code, expected)

    def test_canonical_sample_loads(self) -> None:
        scenario = load_sample(self.sample)
        self.assertEqual(
            scenario.topo_order, ("db", "cache", "api", "edge")
        )
        self.assertEqual(len(scenario.requests), 13)
        self.assertEqual(scenario.conversions[0].scale, 1000)
        self.assertTrue(
            all(conversion.rounding == "floor" for conversion in scenario.conversions)
        )
        self.assertEqual(scenario.policy.safety_ratio_fixed, 1000)
        self.assertEqual(scenario.policy.ratio_denominator, 10000)

    def test_missing_file(self) -> None:
        (self.sample / "policy.json").unlink()
        self.assert_code(E_SAMPLE_MISSING)

    def test_malformed_json(self) -> None:
        (self.sample / "services.json").write_text("{broken", encoding="utf-8")
        self.assert_code(E_JSON_MALFORMED)

    def test_duplicate_name(self) -> None:
        data = read_json(self.sample / "services.json")
        data["services"][1]["name"] = "db"
        write_json(self.sample / "services.json", data)
        self.assert_code(E_NAME_DUPLICATE)

    def test_missing_dependency(self) -> None:
        data = read_json(self.sample / "services.json")
        data["services"][2]["depends_on"].append("ghost")
        write_json(self.sample / "services.json", data)
        self.assert_code(E_DEPENDENCY_MISSING)

    def test_dependency_cycle(self) -> None:
        data = read_json(self.sample / "services.json")
        data["services"][0]["depends_on"] = ["edge"]
        write_json(self.sample / "services.json", data)
        self.assert_code(E_DEPENDENCY_CYCLE)

    def test_pending_service_time_conflict(self) -> None:
        data = read_json(self.sample / "requests.json")
        data["requests"][1]["service"] = "db"
        data["requests"][1]["clock_ms"] = data["requests"][0]["clock_ms"]
        write_json(self.sample / "requests.json", data)
        self.assert_code(E_PENDING_CONFLICT)

    def test_clock_rollback(self) -> None:
        data = read_json(self.sample / "requests.json")
        data["requests"][1]["clock_ms"] = 1
        write_json(self.sample / "requests.json", data)
        self.assert_code(E_CLOCK_ROLLBACK)

    def test_manifest_digest_mismatch(self) -> None:
        data = read_json(self.sample / "services.json")
        data["services"][0]["current_replicas"] = 8
        write_json(self.sample / "services.json", data)
        self.assert_code(E_DIGEST_MISMATCH, require_manifest=True)


if __name__ == "__main__":
    unittest.main()
