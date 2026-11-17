from __future__ import annotations

import copy
import unittest

from tests.helpers import SAMPLES, sample_copy

from shardrouter.config import config_summary, load_config, parse_config
from shardrouter.errors import E_CONFIG, E_RING, RouterError


class ConfigTests(unittest.TestCase):
    def test_sample_config_is_accepted(self) -> None:
        config = load_config(SAMPLES / "config.json")
        summary = config_summary(config)
        self.assertEqual(summary["status"], "ok")
        self.assertEqual([table["name"] for table in summary["tables"]], ["ledger", "orders", "sessions"])
        self.assertEqual(summary["tables"][0]["algorithm"], "range")
        self.assertEqual(summary["tables"][1]["algorithm"], "mod")
        self.assertEqual(summary["tables"][2]["algorithm"], "consistent_hash")

    def test_duplicate_table_name_is_rejected(self) -> None:
        data = sample_copy()
        data["tables"].append(copy.deepcopy(data["tables"][0]))
        with self.assertRaises(RouterError) as raised:
            parse_config(data)
        self.assertEqual(raised.exception.code, E_CONFIG)
        paths = {item["field_path"] for item in raised.exception.details["errors"]}
        self.assertIn("$.tables[3].name", paths)

    def test_missing_shard_key_column_is_rejected(self) -> None:
        data = sample_copy()
        data["tables"][1]["shard_keys"] = ["missing_column"]
        with self.assertRaises(RouterError) as raised:
            parse_config(data)
        self.assertEqual(raised.exception.code, E_CONFIG)
        self.assertIn("$.tables[1].shard_keys[0]", {
            item["field_path"] for item in raised.exception.details["errors"]
        })

    def test_bad_counts_modulus_and_range_are_rejected_together(self) -> None:
        data = sample_copy()
        data["tables"][0]["algorithm"]["boundaries"] = [100, 50]
        data["tables"][1]["database_count"] = 0
        data["tables"][1]["tables_per_database"] = -1
        data["tables"][1]["algorithm"]["modulus"] = 0
        with self.assertRaises(RouterError) as raised:
            parse_config(data)
        error = raised.exception
        self.assertEqual(error.code, E_CONFIG)
        paths = {item["field_path"] for item in error.details["errors"]}
        self.assertIn("$.tables[0].algorithm.boundaries[1]", paths)
        self.assertIn("$.tables[1].database_count", paths)
        self.assertIn("$.tables[1].tables_per_database", paths)
        self.assertIn("$.tables[1].algorithm.modulus", paths)

    def test_non_positive_ring_weight_is_config_error(self) -> None:
        data = sample_copy()
        data["tables"][2]["algorithm"]["nodes"][0]["weight"] = 0
        with self.assertRaises(RouterError) as raised:
            parse_config(data)
        self.assertEqual(raised.exception.code, E_CONFIG)
        self.assertIn("$.tables[2].algorithm.nodes[0].weight", {
            item["field_path"] for item in raised.exception.details["errors"]
        })

    def test_missing_dependency_is_rejected(self) -> None:
        data = sample_copy()
        data["route_units"][1]["depends_on"].append("does_not_exist")
        with self.assertRaises(RouterError) as raised:
            parse_config(data)
        self.assertEqual(raised.exception.code, E_CONFIG)
        self.assertIn("$.route_units[1].depends_on[2]", {
            item["field_path"] for item in raised.exception.details["errors"]
        })

    def test_dependency_cycle_is_rejected(self) -> None:
        data = sample_copy()
        for unit in data["route_units"]:
            if unit["name"] == "orders_lookup":
                unit["depends_on"] = ["orders_rollup"]
        with self.assertRaises(RouterError) as raised:
            parse_config(data)
        self.assertEqual(raised.exception.code, E_CONFIG)
        messages = " ".join(item["message"] for item in raised.exception.details["errors"])
        self.assertIn("dependency cycle", messages)

    def test_duplicate_ring_node_is_e_ring(self) -> None:
        with self.assertRaises(RouterError) as raised:
            load_config(SAMPLES / "anomalies" / "ring_conflict.json")
        self.assertEqual(raised.exception.code, E_RING)
        self.assertEqual(raised.exception.field_path, "$.tables[0].algorithm.nodes")

    def test_cli_style_ring_injection_is_e_ring_without_source_change(self) -> None:
        before = (SAMPLES / "config.json").read_bytes()
        with self.assertRaises(RouterError) as raised:
            load_config(SAMPLES / "config.json", inject_ring_conflict=True)
        after = (SAMPLES / "config.json").read_bytes()
        self.assertEqual(raised.exception.code, E_RING)
        self.assertEqual(before, after)

    def test_broken_json_is_e_config(self) -> None:
        with self.assertRaises(RouterError) as raised:
            load_config(SAMPLES / "anomalies" / "broken_config.json")
        self.assertEqual(raised.exception.code, E_CONFIG)
        self.assertIn("JSON decode error", raised.exception.details["errors"][0]["message"])


if __name__ == "__main__":
    unittest.main()
