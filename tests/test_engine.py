from __future__ import annotations

import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from fixed_join.engine import EngineConfig, execute
from fixed_join.errors import EngineError
from fixed_join.canonical import digest_bytes
from fixed_join.fixed import parse_scale_factor
from fixed_join.loader import load_dataset, load_query
from fixed_join.shuffle import parse_partition_count, shuffle_dataset
from fixed_join.verify import verify_artifacts


TABLES = ROOT / "data" / "sample_tables.json"
QUERY = ROOT / "data" / "sample_query.json"


def run_engine(partitions: int | str = 4, scale_factor: int | str = 100) -> dict:
    return execute(
        EngineConfig(
            tables_path=TABLES,
            query_path=QUERY,
            partitions=partitions,
            scale_factor=scale_factor,
        )
    )


class EngineTests(unittest.TestCase):
    def test_fixed_join_and_aggregate_contract(self) -> None:
        report = run_engine()
        self.assertEqual(report["status"], "OK")
        self.assertEqual(len(report["rows"]), 13)
        self.assertEqual(report["join"]["quantized_reference_collision_count"], 2)
        self.assertEqual(report["aggregate"]["count_total"], 13)
        self.assertEqual(report["aggregate"]["count_non_null"], 13)
        self.assertEqual(report["aggregate"]["count_null"], 0)
        self.assertEqual(report["aggregate"]["sum_fixed_lattice"], 19575)
        self.assertEqual(report["aggregate"]["sum_error_bound_lattice"], 13)
        self.assertEqual(report["aggregate"]["mean_fixed_lattice"], 1505)
        self.assertEqual(report["aggregate"]["mean_error_bound_lattice"], 14)
        self.assertEqual(sum(item["matched_pair_count"] for item in report["join"]["partition_statistics"]), 13)
        self.assertEqual(sum(item["predicate_boundary_mismatch_count"] for item in report["join"]["partition_statistics"]), 1)

    def test_null_join_keys_never_match(self) -> None:
        report = run_engine()
        keys = {(row["left_row_index"], row["right_row_index"]) for row in report["rows"]}
        self.assertFalse(any(left == 5 for left, _ in keys))
        self.assertFalse(any(right == 12 for _, right in keys))
        self.assertGreater(sum(item["left_null_join_keys"] for item in report["join"]["partition_statistics"]), 0)
        self.assertGreater(sum(item["right_null_join_keys"] for item in report["join"]["partition_statistics"]), 0)

    def test_quantized_collision_is_explicit(self) -> None:
        report = run_engine()
        collisions = report["join"]["quantized_reference_collisions"]
        self.assertEqual(len(collisions), 2)
        for collision in collisions:
            self.assertEqual(collision["join_key_fixed_lattice"], 123)
            self.assertNotEqual(collision["left_reference"], collision["right_reference"])
        collision_rows = [row for row in report["rows"] if row["join_key"]["reference_collision"]]
        self.assertEqual(len(collision_rows), 2)

    def test_shuffle_digests_are_input_row_order_independent(self) -> None:
        dataset = load_dataset(TABLES)
        query = load_query(QUERY, dataset)
        fixed = parse_scale_factor(100)
        partitions = parse_partition_count(4)
        first = shuffle_dataset(dataset, query, fixed, partitions)
        reversed_tables = tuple(replace(table, rows=tuple(reversed(table.rows))) for table in dataset.tables)
        second = shuffle_dataset(replace(dataset, tables=reversed_tables), query, fixed, partitions)
        self.assertEqual(first.global_digest, second.global_digest)
        self.assertEqual(
            [item.combined_digest for item in first.partitions],
            [item.combined_digest for item in second.partitions],
        )

    def test_attribution_is_partition_count_independent(self) -> None:
        single = run_engine(partitions=1)
        multi = run_engine(partitions=4)
        single_map = {
            row["row_id"]: (
                row["error_attribution"]["primary_source"],
                row["error_attribution"]["quantization_bound_lattice"],
                row["error_attribution"]["aggregation_prefix_bound_lattice"],
            )
            for row in single["rows"]
        }
        multi_map = {
            row["row_id"]: (
                row["error_attribution"]["primary_source"],
                row["error_attribution"]["quantization_bound_lattice"],
                row["error_attribution"]["aggregation_prefix_bound_lattice"],
            )
            for row in multi["rows"]
        }
        self.assertEqual(single_map, multi_map)

    def test_repeated_runs_are_byte_equivalent_for_core_results(self) -> None:
        first = run_engine()
        second = run_engine()
        self.assertEqual(first["rows"], second["rows"])
        self.assertEqual(first["aggregate"], second["aggregate"])
        self.assertEqual(first["shuffle"], second["shuffle"])
        self.assertEqual(first["determinism"], second["determinism"])

    def test_scale_and_partition_errors(self) -> None:
        for value in (0, -10, 25, 10**10):
            with self.assertRaises(EngineError) as context:
                run_engine(scale_factor=value)
            self.assertEqual(context.exception.code, "E_SCALEF")
        for value in (0, -1, 65, "4x"):
            with self.assertRaises(EngineError) as context:
                run_engine(partitions=value)
            self.assertEqual(context.exception.code, "E_PART")

    def test_invalid_table_and_nullmap_codes(self) -> None:
        payload = json.loads(TABLES.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = Path(temporary)
            bad_range = json.loads(json.dumps(payload))
            bad_range["tables"][0]["rows"][0]["row_id"] = 999
            range_file = temporary_path / "bad_range.json"
            range_file.write_text(json.dumps(bad_range), encoding="utf-8")
            with self.assertRaises(EngineError) as context:
                execute(EngineConfig(range_file, QUERY, 4, 100))
            self.assertEqual(context.exception.code, "E_TABLE")
            self.assertEqual(context.exception.table, "measurements")
            self.assertEqual(context.exception.row, 1)
            self.assertEqual(context.exception.column, "row_id")

            bad_bitmap = json.loads(json.dumps(payload))
            bad_bitmap["tables"][0]["null_bitmap"]["sensor_key"].pop()
            bitmap_file = temporary_path / "bad_bitmap.json"
            bitmap_file.write_text(json.dumps(bad_bitmap), encoding="utf-8")
            with self.assertRaises(EngineError) as context:
                execute(EngineConfig(bitmap_file, QUERY, 4, 100))
            self.assertEqual(context.exception.code, "E_NULLMAP")
            self.assertEqual(context.exception.parent_code, "E_TABLE")
            self.assertEqual(context.exception.table, "measurements")
            self.assertEqual(context.exception.column, "sensor_key")

    def test_verifier_recomputes_aggregate_and_attribution(self) -> None:
        first = run_engine(partitions=1)
        second = run_engine(partitions=4)
        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = Path(temporary)
            first_path = temporary_path / "first.json"
            second_path = temporary_path / "second.json"
            first_path.write_text(json.dumps(first), encoding="utf-8")
            second_path.write_text(json.dumps(second), encoding="utf-8")
            verified = verify_artifacts(TABLES, QUERY, [first_path, second_path])
            self.assertEqual(verified["status"], "OK")
            self.assertEqual(verified["checked_reports"], 2)

            tampered = json.loads(json.dumps(first))
            tampered["aggregate"]["sum_fixed_lattice"] += 1
            tampered_path = temporary_path / "tampered.json"
            tampered_path.write_text(json.dumps(tampered), encoding="utf-8")
            result = verify_artifacts(TABLES, QUERY, [tampered_path])
            self.assertEqual(result["status"], "ERROR")
            self.assertEqual(result["code"], "E_VERIFY")
            self.assertEqual(result["first_deviation"]["item"], "aggregate.sum_fixed_lattice")

            tampered_attr = json.loads(json.dumps(first))
            tampered_attr["rows"][0]["error_attribution"]["primary_source"] = "aggregation"
            tampered_attr["determinism"]["output_digest"] = digest_bytes(tampered_attr["rows"])
            attr_path = temporary_path / "tampered_attr.json"
            attr_path.write_text(json.dumps(tampered_attr), encoding="utf-8")
            attr_result = verify_artifacts(TABLES, QUERY, [attr_path])
            self.assertEqual(attr_result["status"], "ERROR")
            self.assertEqual(attr_result["code"], "E_ATTR")


if __name__ == "__main__":
    unittest.main()
