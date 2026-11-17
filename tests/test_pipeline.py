from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from colpipe.catalog import load_pipeline_dict
from colpipe.codes import E_BUDGET, E_DATA, E_DIVERGE, E_PARSE, E_VERSION
from colpipe.errors import PipeError
from colpipe.executor import compare_results, execute
from colpipe.plan import build_plans, walk_plan
from colpipe.sample import divergent_payload, sample_payload
from colpipe.snapshot import create_snapshot, load_snapshot, restore_snapshot, save_snapshot
from colpipe.util import sha256_bytes, sha256_json, write_json
from colpipe.verifier import verify_snapshot


class PipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.payload = sample_payload()
        self.request = load_pipeline_dict(copy.deepcopy(self.payload))
        self.bundle = build_plans(self.request)

    def test_loader_rejects_bad_bitmap_with_field_path(self) -> None:
        payload = copy.deepcopy(self.payload)
        payload["tables"][0]["columns"][0]["null_bitmap"].pop()
        with self.assertRaises(PipeError) as caught:
            load_pipeline_dict(payload)
        self.assertEqual(caught.exception.code, E_DATA)
        self.assertIn("$.tables[0].columns[0].null_bitmap", caught.exception.path)

    def test_loader_rejects_duplicate_table_and_missing_query_column(self) -> None:
        payload = copy.deepcopy(self.payload)
        payload["tables"].append(copy.deepcopy(payload["tables"][0]))
        with self.assertRaises(PipeError) as duplicate:
            load_pipeline_dict(payload)
        self.assertEqual(duplicate.exception.code, E_DATA)
        self.assertIn("duplicate table name", duplicate.exception.message)

        payload = copy.deepcopy(self.payload)
        payload["query"] = payload["query"].replace("SUM(o.amount)", "SUM(o.not_a_column)")
        with self.assertRaises(PipeError) as missing:
            load_pipeline_dict(payload)
        self.assertEqual(missing.exception.code, E_DATA)
        self.assertIn("not_a_column", missing.exception.message)

    def test_parser_normalises_whitespace_and_parentheses(self) -> None:
        payload = copy.deepcopy(self.payload)
        payload["query"] = (
            "\n SELECT c.region AS region, o.category AS category, "
            "SUM(o.amount) AS total_amount, COUNT(*) AS order_count, MAX(o.quantity) AS max_quantity\n"
            "FROM orders AS o INNER JOIN customers AS c ON (o.customer_id = c.id)\n"
            "WHERE ((o.amount >= 25.0) AND ((o.amount < 90.0)))\n"
            " AND (o.category IN ['books', 'games']) AND (c.active = true)\n"
            "GROUP BY c.region, o.category ORDER BY region, category"
        )
        request = load_pipeline_dict(payload)
        self.assertEqual(request.query.summary_sha256, self.request.query.summary_sha256)
        self.assertEqual(build_plans(request).optimized_summary_sha256, self.bundle.optimized_summary_sha256)

    def test_parser_rejects_or_with_e_parse(self) -> None:
        payload = copy.deepcopy(self.payload)
        payload["query"] = payload["query"].replace("AND c.active = true", "OR c.active = true")
        with self.assertRaises(PipeError) as caught:
            load_pipeline_dict(payload)
        self.assertEqual(caught.exception.code, E_PARSE)
        self.assertIn("OR", caught.exception.message)

    def test_pushdown_and_column_pruning_are_reported(self) -> None:
        pushed = [
            item["condition"]["column"] for item in self.bundle.pushdown["pushed_conditions"]
        ]
        self.assertEqual(pushed, ["o.amount", "o.amount", "o.category", "c.active"])
        scan_nodes = {
            node["attrs"]["alias"]: node for node in walk_plan(self.bundle.optimized_plan) if node["op"] == "Scan"
        }
        self.assertEqual(
            [column["column"] for column in scan_nodes["o"]["attrs"]["columns"]],
            ["customer_id", "amount", "category", "quantity"],
        )
        self.assertEqual(
            [column["column"] for column in scan_nodes["c"]["attrs"]["columns"]],
            ["id", "region", "active"],
        )
        dropped = {item["column"] for item in self.bundle.pruning["dropped_columns"]}
        self.assertIn("o.order_id", dropped)
        self.assertIn("o.channel", dropped)
        self.assertIn("c.note", dropped)
        self.assertNotIn("o.amount", dropped)
        self.assertIn("type descriptor and null bitmap", self.bundle.pruning["derivation"]["null_semantics"])

    def test_fixed_point_estimate_and_split_are_deterministic(self) -> None:
        second = build_plans(load_pipeline_dict(copy.deepcopy(self.payload)))
        self.assertEqual(second.split_plan, self.bundle.split_plan)
        self.assertEqual(second.estimates["split_sha256"], self.bundle.estimates["split_sha256"])
        for scan in self.bundle.estimates["scans"].values():
            estimate = scan["estimate"]
            self.assertIsInstance(estimate["selectivity_ppm"], int)
            self.assertIsInstance(estimate["estimated_output_rows"], int)
            self.assertIsInstance(estimate["quantization_error_rows_upper_bound"], int)
            self.assertLessEqual(scan["task_count"], self.request.settings.parallelism)
            cursor = 0
            for task in scan["tasks"]:
                start, end = task["input_range"]
                self.assertEqual(start, cursor)
                self.assertLessEqual(end, scan["estimate"]["row_count"])
                cursor = end
            self.assertEqual(cursor, scan["estimate"]["row_count"])

    def test_parallel_execution_respects_memory_and_parallel_budget(self) -> None:
        execution = execute(self.bundle, mode="optimized")
        budget = self.request.settings.memory_budget_bytes
        parallelism = self.request.settings.parallelism
        events = []
        for task in execution.trace:
            self.assertLessEqual(task["start_ms"], task["end_ms"])
            self.assertGreaterEqual(task["budget_queue_wait_ms"], 0)
            memory = task["peak_memory_bytes"]
            events.append((task["start_ms"], 1, memory))
            events.append((task["end_ms"], 0, -memory))
        events.sort(key=lambda item: (item[0], item[1]))
        active_memory = 0
        active_tasks = 0
        peak_memory = 0
        max_tasks = 0
        for _time, kind, delta in events:
            active_memory += delta
            active_tasks += 1 if kind == 1 else -1
            peak_memory = max(peak_memory, active_memory)
            max_tasks = max(max_tasks, active_tasks)
            self.assertLessEqual(active_memory, budget)
            self.assertLessEqual(active_tasks, parallelism)
        self.assertLessEqual(execution.stats["peak_scheduler_reserved_memory_bytes"], budget)
        self.assertGreaterEqual(peak_memory, 0)
        self.assertLessEqual(max_tasks, parallelism)

    def test_single_task_budget_failure(self) -> None:
        payload = copy.deepcopy(self.payload)
        payload["settings"]["memory_budget_bytes"] = 4096
        bundle = build_plans(load_pipeline_dict(payload))
        with self.assertRaises(PipeError) as caught:
            execute(bundle, mode="optimized")
        self.assertEqual(caught.exception.code, E_BUDGET)

    def test_optimized_and_baseline_are_byte_equal_with_consistent_stats(self) -> None:
        optimized = execute(self.bundle, mode="optimized")
        baseline = execute(self.bundle, mode="baseline")
        comparison = compare_results(optimized, baseline)
        self.assertTrue(comparison["byte_equal"])
        self.assertTrue(comparison["statistics_consistent"]["ok"])
        self.assertEqual(optimized.canonical_bytes, baseline.canonical_bytes)
        self.assertNotEqual(optimized.stats["scan_output_rows"], baseline.stats["scan_output_rows"])
        self.assertEqual(optimized.stats["aggregate_input_rows"], baseline.stats["aggregate_input_rows"])

    def test_same_named_columns_survive_join_projection(self) -> None:
        payload = {
            "format": "colpipe.input.v1",
            "session_id": "same-name",
            "data_version": "v1",
            "tables": [
                {
                    "name": "a",
                    "schema_version": "1",
                    "row_count": 3,
                    "columns": [
                        {"name": "id", "type": "int64", "values": [1, 2, 3], "null_bitmap": [False] * 3},
                        {"name": "code", "type": "string", "values": ["x", "y", "z"], "null_bitmap": [False] * 3},
                    ],
                },
                {
                    "name": "b",
                    "schema_version": "1",
                    "row_count": 3,
                    "columns": [
                        {"name": "id", "type": "int64", "values": [1, 2, 3], "null_bitmap": [False] * 3},
                        {"name": "code", "type": "string", "values": ["p", "q", "r"], "null_bitmap": [False] * 3},
                    ],
                },
            ],
            "query": (
                "SELECT a.code AS acode, b.code AS bcode, COUNT(*) AS n "
                "FROM a JOIN b ON a.id = b.id GROUP BY a.code, b.code ORDER BY acode, bcode"
            ),
            "settings": {"parallelism": 2, "memory_budget_bytes": 100000, "min_rows_per_task": 1},
        }
        bundle = build_plans(load_pipeline_dict(payload))
        optimized = execute(bundle, mode="optimized")
        baseline = execute(bundle, mode="baseline")
        self.assertEqual(optimized.rows, [["x", "p", 1], ["y", "q", 1], ["z", "r", 1]])
        self.assertTrue(compare_results(optimized, baseline)["byte_equal"])

    def test_divergence_reports_first_group_key_and_field(self) -> None:
        optimized = execute(self.bundle, mode="optimized")
        baseline = execute(self.bundle, mode="baseline")
        baseline.rows[0][2] = float(baseline.rows[0][2]) + 1.0
        baseline.canonical_bytes = b"diverged-for-test"
        baseline.canonical_sha256 = sha256_bytes(baseline.canonical_bytes)
        with self.assertRaises(PipeError) as caught:
            compare_results(optimized, baseline)
        self.assertEqual(caught.exception.code, E_DIVERGE)
        difference = caught.exception.details["first_difference"]
        self.assertEqual(difference["field"], "total_amount")
        self.assertIn("c.region", difference["group_key"])

    def test_snapshot_restore_replaces_state_and_rejects_wrong_session(self) -> None:
        optimized = execute(self.bundle, mode="optimized")
        baseline = execute(self.bundle, mode="baseline")
        comparison = compare_results(optimized, baseline)
        snapshot = create_snapshot(self.bundle, {"optimized": optimized, "baseline": baseline}, comparison)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.json"
            save_snapshot(path, snapshot)
            loaded = load_snapshot(path)
            self.assertEqual(loaded["snapshot_id"], snapshot["snapshot_id"])
        mutated_payload = divergent_payload()
        extra = copy.deepcopy(mutated_payload["tables"][0])
        extra["name"] = "extra_table"
        mutated_payload["tables"].append(extra)
        mutated = load_pipeline_dict(mutated_payload)
        restored = restore_snapshot(snapshot, mutated)
        self.assertEqual(restored.request.dataset.data_version, "sales-2026-11-17.v1")
        self.assertEqual({table.name for table in restored.request.dataset.tables}, {"customers", "orders"})
        self.assertEqual(restored.report["mode"], "replace_all_not_merge")
        self.assertTrue(restored.report["version_changed"])
        self.assertTrue(restored.report["statistics_replaced"])
        self.assertTrue(any(item["kind"] == "table_data_changed" for item in restored.report["differences"]))
        self.assertTrue(any(item["kind"] == "table_removed" for item in restored.report["differences"]))
        self.assertEqual(len(restored.plan_history), 2)

        other = copy.deepcopy(self.payload)
        other["session_id"] = "different-session"
        with self.assertRaises(PipeError) as caught:
            restore_snapshot(snapshot, load_pipeline_dict(other))
        self.assertEqual(caught.exception.code, E_VERSION)
        self.assertTrue(caught.exception.details["differences"])

    def test_verifier_passes_and_detects_tampered_embedded_result(self) -> None:
        optimized = execute(self.bundle, mode="optimized")
        baseline = execute(self.bundle, mode="baseline")
        comparison = compare_results(optimized, baseline)
        snapshot = create_snapshot(self.bundle, {"optimized": optimized, "baseline": baseline}, comparison)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.json"
            save_snapshot(path, snapshot)
            self.assertEqual(verify_snapshot(path)["status"], "PASS")

            tampered = copy.deepcopy(snapshot)
            tampered["executions"]["optimized"]["result"]["rows"][0][2] = 123456.0
            tampered["snapshot_id"] = sha256_json(
                {key: value for key, value in tampered.items() if key != "snapshot_id"}
            )
            write_json(path, tampered)
            result = verify_snapshot(path)
            self.assertEqual(result["status"], "FAIL")
            self.assertIn(result["code"], {"E_DIVERGE", "E_VERSION"})


if __name__ == "__main__":
    unittest.main()
