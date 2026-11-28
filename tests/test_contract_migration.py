from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from contract_migration.edges import build_edge_cases
from contract_migration.generator import generate_dataset
from contract_migration.hotspot import detect_hotspots
from contract_migration.model import contract_id, contract_index
from contract_migration.planner import build_migration_plan
from contract_migration.workflow import (
    resume_from_snapshot,
    run_scenario,
    write_json_atomic,
)


ROOT = Path(__file__).resolve().parents[1]


def _independent_route(segments: list[dict], key: str) -> str:
    expanded: dict[str, str] = {}
    for segment in segments:
        for index in range(
            contract_index(segment["start"]), contract_index(segment["end"]) + 1
        ):
            expanded[contract_id(index)] = segment["shard"]
    return expanded[key]


class ContractMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.report = run_scenario(snapshot_path=None)

    def test_generation_reconciliation_and_event_join(self) -> None:
        dataset = generate_dataset()
        self.assertEqual(len(dataset.contracts), 240)
        self.assertGreater(len(dataset.seal_usages), 240)
        self.assertGreater(len(dataset.signing_events), 240)
        contracts = dataset.contracts_by_id
        self.assertTrue(
            all(seal.contract_id in contracts for seal in dataset.seal_usages)
        )
        self.assertTrue(
            all(event.contract_id in contracts for event in dataset.signing_events)
        )
        reconciliation = self.report["capacity_reconciliation"]
        self.assertTrue(reconciliation["balanced"])
        self.assertEqual(
            reconciliation["total_document_bytes"],
            reconciliation["total_declared_used_bytes"],
        )
        self.assertTrue(
            all(row["delta_bytes"] == 0 for row in reconciliation["per_shard"])
        )
        again = generate_dataset()
        self.assertEqual(dataset.shards, again.shards)
        self.assertEqual(dataset.contracts, again.contracts)
        self.assertEqual(dataset.seal_usages, again.seal_usages)
        self.assertEqual(dataset.signing_events, again.signing_events)
        self.assertEqual(
            dataset.observation_requests, again.observation_requests
        )

    def test_sliding_hotspot_truth_and_cross_boundary_split(self) -> None:
        hotspot = self.report["hotspot_analysis"]
        self.assertEqual(
            hotspot["window_sequence"], {"start": 40, "end": 159}
        )
        self.assertEqual(hotspot["hot_range"]["start"], "HT-000038")
        self.assertEqual(hotspot["hot_range"]["end"], "HT-000082")
        self.assertEqual(hotspot["hot_shard_ids"], ["shard-a", "shard-b"])
        self.assertEqual(len(hotspot["hot_range_slices"]), 2)
        self.assertTrue(
            all(
                slice_row["split_from_cross_boundary"]
                for slice_row in hotspot["hot_range_slices"]
            )
        )
        self.assertEqual(
            [row["source_shard"] for row in hotspot["hot_range_slices"]],
            ["shard-a", "shard-b"],
        )
        self.assertTrue(hotspot["truth_check"]["range_matches"])
        self.assertTrue(hotspot["truth_check"]["shards_match"])
        self.assertTrue(hotspot["truth_check"]["slices_match"])
        self.assertTrue(hotspot["truth_check"]["customer_batches_match"])
        self.assertEqual(
            hotspot["hot_range"]["customer_batches"], ["BATCH-HOT-07"]
        )

    def test_migration_plan_capacity_and_low_water_targets(self) -> None:
        dataset = generate_dataset()
        hotspot = detect_hotspots(dataset, 120, 8_000)
        plan = build_migration_plan(dataset, hotspot, 220_000)
        contracts = dataset.contracts_by_id
        for slice_plan in plan["plans"]:
            self.assertEqual(slice_plan["status"], "accepted")
            actual_bytes = sum(
                contracts[key].document_bytes for key in slice_plan["contract_ids"]
            )
            self.assertEqual(actual_bytes, slice_plan["required_bytes"])
            eligible = [
                row
                for row in slice_plan["target_selection"]["candidates"]
                if row["eligible"]
            ]
            selected = next(
                row
                for row in slice_plan["target_selection"]["candidates"]
                if row["selected"]
            )
            self.assertEqual(
                selected["current_watermark_bp"],
                min(row["current_watermark_bp"] for row in eligible),
            )
            self.assertLessEqual(
                selected["current_used_bytes"] + actual_bytes,
                selected["capacity_bytes"],
            )
        for batch in plan["flattened_batches"]:
            self.assertLessEqual(batch["bytes"], 220_000)
            self.assertTrue(batch["within_batch_capacity"])
            self.assertEqual(
                sum(contracts[key].document_bytes for key in batch["contracts"]),
                batch["bytes"],
            )

    def test_each_request_against_independent_reference_router(self) -> None:
        route_by_epoch = {
            0: self.report["initial_route_table"],
        }
        for switch in self.report["batch_switch_history"]:
            route_by_epoch[switch["route_epoch"]] = switch["route_change"]["after"]
        checked = 0
        for record in self.report["access_routing"]["records"]:
            expected = _independent_route(
                route_by_epoch[record["route_epoch"]], record["contract_id"]
            )
            self.assertEqual(
                record["routed_shard"],
                expected,
                msg=f"reference mismatch at {record['request_id']}",
            )
            self.assertTrue(record["reference_match"])
            checked += 1
        self.assertEqual(
            checked, self.report["access_routing"]["migration_request_count"]
        )
        self.assertEqual(
            self.report["access_routing"]["reference_router_mismatch_count"], 0
        )

    def test_write_visibility_and_source_reclaim(self) -> None:
        routing = self.report["access_routing"]
        self.assertGreater(routing["migrating_write_count"], 0)
        self.assertTrue(routing["all_migrating_writes_have_equal_versions"])
        self.assertGreater(routing["read_during_migration_count"], 0)
        self.assertEqual(routing["pending_reclaim_service_violation_count"], 0)
        reclaim = self.report["source_reclaim_state"]
        self.assertEqual(reclaim["row_count"], 45)
        self.assertTrue(reclaim["all_source_keys_pending_reclaim"])
        self.assertTrue(reclaim["all_routes_point_to_target"])
        self.assertTrue(self.report["hotspot_elimination"]["hotspot_eliminated"])
        self.assertEqual(
            self.report["hotspot_elimination"]["post_hot_shards"], []
        )

    def test_interruption_resume_is_equivalent_to_full_migration(self) -> None:
        resume = self.report["interruption_resume"]
        self.assertEqual(
            resume["interruption"]["snapshot"]["completed_batches"],
            ["batch-01"],
        )
        self.assertEqual(resume["resume"]["completed_batches_replayed"], 0)
        self.assertEqual(
            resume["resume"]["contracts_already_synced_loaded"],
            resume["interruption"]["snapshot"][
                "active_batch_synced_contracts"
            ],
        )
        self.assertLess(
            resume["interruption"]["interrupt_after_contracts_synced"],
            resume["interruption"]["active_batch_contract_count"],
        )
        self.assertGreater(
            resume["interruption"]["active_batch_contracts_not_yet_synced"], 0
        )
        self.assertEqual(
            resume["resume"]["contract_syncs_skipped_as_already_complete"], 3
        )
        equivalence = resume["equivalence"]
        self.assertTrue(equivalence["equivalent"])
        self.assertTrue(equivalence["route_table_equal"])
        self.assertTrue(equivalence["key_state_and_versions_equal"])
        self.assertEqual(equivalence["per_contract_mismatch_count"], 0)
        self.assertEqual(equivalence["contracts_compared"], 240)

    def test_constructed_boundary_cases(self) -> None:
        edge = self.report["edge_cases"]
        self.assertTrue(edge["all_passed"])
        names = {case["case"] for case in edge["cases"]}
        self.assertIn("hot_range_exactly_crosses_route_boundary", names)
        self.assertIn(
            "concurrent_read_write_same_contract_during_migration", names
        )
        self.assertIn("disconnect_immediately_after_batch_switch", names)
        self.assertIn("target_reaches_capacity_exactly", names)
        self.assertIn("target_capacity_insufficient", names)
        self.assertEqual(
            edge["error_codes"]["TARGET_CAPACITY_INSUFFICIENT"], 1012
        )

    def test_report_bytes_are_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "first.json"
            second = Path(directory) / "second.json"
            first_report = run_scenario(snapshot_path=None)
            second_report = run_scenario(snapshot_path=None)
            write_json_atomic(first, first_report)
            write_json_atomic(second, second_report)
            self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_snapshot_file_can_be_resumed_independently(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory) / "snapshot.json"
            run_scenario(snapshot_path=str(snapshot))
            self.assertTrue(snapshot.exists())
            resumed = resume_from_snapshot(snapshot)
            self.assertTrue(
                resumed["equivalence_to_full_migration"]["equivalent"]
            )
            self.assertTrue(resumed["consistency_checks"]["all_passed"])

    def test_cli_injects_integer_error_codes(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "contract_migration",
                "--inject-error",
                "1008",
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 2)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error_code"], 1008)
        self.assertEqual(payload["symbol"], "CAPACITY_EXCEEDED")

    def test_cli_rejects_invalid_parameter_with_symbolic_code(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "contract_migration",
                "--seed",
                "-1",
                "--snapshot",
                "-",
                "--output",
                "-",
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 2)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["error_code"], 1001)
        self.assertEqual(payload["symbol"], "INVALID_SEED")


if __name__ == "__main__":
    unittest.main()
