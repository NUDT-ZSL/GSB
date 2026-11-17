from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from lbhealth.config import load_config, parse_config
from lbhealth.errors import PoolValidationError

from .helpers import base_raw, make_config


class ConfigValidationTests(unittest.TestCase):
    def assert_pool_error(self, raw: dict, path: str) -> None:
        with self.assertRaises(PoolValidationError) as caught:
            parse_config(raw)
        self.assertEqual(caught.exception.code, "E_POOL")
        paths = [item["path"] for item in caught.exception.errors]
        self.assertIn(path, paths, caught.exception.to_dict())

    def test_valid_mixed_protocol_config(self) -> None:
        config = make_config()
        self.assertEqual(config.config_version, "test-v1")
        self.assertEqual([member.protocol for member in config.members], ["http", "tcp"])
        self.assertEqual(config.pool.full_weight_sum, 100)
        self.assertEqual(len(config.member_digest), 64)

    def test_non_loopback_and_hostname_are_rejected(self) -> None:
        for address in ("192.168.1.10", "localhost", "0.0.0.0", "example.com"):
            with self.subTest(address=address):
                raw = base_raw()
                raw["pool"]["members"][0]["address"] = address
                self.assert_pool_error(raw, "$.pool.members[0].address")

    def test_port_and_protocol_validation(self) -> None:
        cases = [
            (0, "$.pool.members[0].port"),
            (65536, "$.pool.members[0].port"),
            (True, "$.pool.members[0].port"),
            ("80", "$.pool.members[0].port"),
        ]
        for value, path in cases:
            with self.subTest(value=value):
                raw = base_raw()
                raw["pool"]["members"][0]["port"] = value
                self.assert_pool_error(raw, path)
        raw = base_raw()
        raw["pool"]["members"][0]["protocol"] = "udp"
        self.assert_pool_error(raw, "$.pool.members[0].protocol")

    def test_duplicate_id_path_uses_original_member_index(self) -> None:
        raw = base_raw()
        raw["pool"]["members"][0]["weight"] = -1
        raw["pool"]["members"].append(dict(raw["pool"]["members"][1]))
        with self.assertRaises(PoolValidationError) as caught:
            parse_config(raw)
        paths = [item["path"] for item in caught.exception.errors]
        self.assertIn("$.pool.members[0].weight", paths)
        self.assertIn("$.pool.members[2].id", paths)

    def test_timing_and_thresholds(self) -> None:
        cases = [
            ("interval_ms", 0, "$.pool.interval_ms"),
            ("interval_ms", True, "$.pool.interval_ms"),
            ("timeout_ms", 0, "$.pool.timeout_ms"),
            ("timeout_ms", 500, "$.pool.timeout_ms"),
            ("success_threshold", 0, "$.pool.success_threshold"),
            ("failure_threshold", -1, "$.pool.failure_threshold"),
        ]
        for field, value, path in cases:
            with self.subTest(field=field, value=value):
                raw = base_raw()
                raw["pool"][field] = value
                self.assert_pool_error(raw, path)

    def test_weights_and_slow_start_table(self) -> None:
        raw = base_raw()
        raw["pool"]["members"][0]["weight"] = -1
        self.assert_pool_error(raw, "$.pool.members[0].weight")
        raw = base_raw()
        raw["pool"]["members"][0]["weight"] = 0
        raw["pool"]["members"][1]["weight"] = 0
        self.assert_pool_error(raw, "$.pool.members")
        cases = [
            ([], "$.pool.slow_start_weight_table"),
            ([25, 50, 100, 100], "$.pool.slow_start_weight_table"),
            ([25, 50, 75], "$.pool.slow_start_weight_table"),
            ([25, 101, 100], "$.pool.slow_start_weight_table[1]"),
            ([25, 50, 25], "$.pool.slow_start_weight_table"),
        ]
        for table, expected_path in cases:
            with self.subTest(table=table):
                raw = base_raw()
                raw["pool"]["slow_start_weight_table"] = table
                self.assert_pool_error(raw, expected_path)

    def test_http_path_injection_is_rejected(self) -> None:
        for path in ("health", "/health\r\nX-Evil: 1", "/health test"):
            with self.subTest(path=path):
                raw = base_raw()
                raw["pool"]["members"][0]["probe_path"] = path
                self.assert_pool_error(raw, "$.pool.members[0].probe_path")

    def test_malformed_json_is_e_pool(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "bad.json"
            path.write_text('{"pool":', encoding="utf-8")
            with self.assertRaises(PoolValidationError) as caught:
                load_config(path)
            self.assertEqual(caught.exception.errors[0]["path"], "$")

    def test_original_file_load_is_deterministic(self) -> None:
        config = load_config(Path(__file__).resolve().parents[1] / "config" / "sample_pool.json")
        self.assertEqual(config.member_digest, "51468bf498c536186bde825a7bcb6d53cb4bb5a9df7b5ea07a304052bda444e8")


if __name__ == "__main__":
    unittest.main()
