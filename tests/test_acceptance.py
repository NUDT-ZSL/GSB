from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from moe_capacity import (
    canonical_fingerprint,
    generate_project_set,
    normalize_document,
    run_allocation,
    run_reference,
    verify_project_set,
)
from moe_capacity.reference import compare_with_reference


class NormalizationTests(unittest.TestCase):
    def test_three_versions_share_one_canonical_fingerprint(self) -> None:
        documents = generate_project_set(seed=42, request_count=24)
        projects = {
            version: normalize_document(document)
            for version, document in documents.items()
        }
        fingerprints = {
            canonical_fingerprint(project) for project in projects.values()
        }
        self.assertEqual(len(fingerprints), 1)
        e0 = {
            version: next(
                expert
                for expert in project.experts
                if expert.expert_id == "e0"
            )
            for version, project in projects.items()
        }
        self.assertEqual(
            {expert.cross_rack_comm_bytes for expert in e0.values()},
            {6 * 1024 * 1024},
        )
        self.assertEqual(
            {
                project.normalization["sample_equivalent_bytes"]
                for project in projects.values()
            },
            {4096},
        )

    def test_flat_and_nested_expert_tables_are_aligned(self) -> None:
        documents = generate_project_set(seed=11, request_count=12)
        projects = {
            version: normalize_document(document)
            for version, document in documents.items()
        }
        expert_rows = {
            version: [
                (
                    expert.expert_id,
                    expert.home_replica,
                    expert.domains,
                    expert.per_sample_cost_bytes,
                    expert.cross_rack_comm_bytes,
                    expert.precision,
                )
                for expert in sorted(
                    project.experts, key=lambda item: item.expert_id
                )
            ]
            for version, project in projects.items()
        }
        self.assertEqual(expert_rows["v1"], expert_rows["v2"])
        self.assertEqual(expert_rows["v1"], expert_rows["v3"])


class RoutingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.documents = generate_project_set(seed=42, request_count=24)
        cls.verification = verify_project_set(cls.documents)
        cls.projects = {
            version: normalize_document(document)
            for version, document in cls.documents.items()
        }
        cls.routes = {
            version: cls.verification["versions"][version]["route"]
            for version in ("v1", "v2", "v3")
        }

    def test_acceptance_passes(self) -> None:
        acceptance = self.verification["acceptance"]
        self.assertTrue(acceptance["passed"], acceptance)
        self.assertTrue(acceptance["cross_version_consistent"])
        self.assertTrue(acceptance["capacity_constraints_satisfied"])
        self.assertTrue(acceptance["reference_allocator_agrees_for_all_versions"])

    def test_reference_agrees_request_by_request(self) -> None:
        for version, project in self.projects.items():
            main = self.routes[version]
            reference = run_reference(project)
            check = compare_with_reference(main, reference)
            self.assertTrue(check["agrees"], (version, check))
            self.assertEqual(check["requests_checked"], len(project.requests))
            self.assertIsNone(check["first_mismatch_request"])
            self.assertTrue(check["ledger_equal"])
            self.assertTrue(check["metrics_equal"])

    def test_routes_are_capacity_feasible(self) -> None:
        for version, route in self.routes.items():
            for replica in route["after_rebalance"]["ledger"]["replicas"]:
                self.assertLessEqual(
                    replica["weighted_load_bytes"],
                    replica["capacity_bytes"],
                    (version, replica["replica_id"]),
                )

    def test_same_rack_dispatch_is_not_billed(self) -> None:
        assignments = {
            row["request_id"]: row for row in self.routes["v1"]["assignments"]
        }
        self.assertTrue(assignments)
        saw_same_rack_avoidance = False
        for row in assignments.values():
            if row["same_rack_dispatch"]:
                self.assertEqual(row["billed_communication_bytes"], 0)
                if row["cross_replica_raw_bytes"]:
                    saw_same_rack_avoidance = True
            else:
                self.assertEqual(
                    row["billed_communication_bytes"],
                    row["cross_replica_raw_bytes"],
                )
        self.assertTrue(saw_same_rack_avoidance)

    def test_ledger_merges_experts_on_a_replica(self) -> None:
        assignments = {
            row["request_id"]: row for row in self.routes["v1"]["assignments"]
        }
        for replica in self.routes["v1"]["after_rebalance"]["ledger"]["replicas"]:
            expected_ids = sorted(
                request_id
                for request_id, row in assignments.items()
                if row["target_replica"] == replica["replica_id"]
            )
            actual_ids = sorted(
                request_id
                for expert in replica["experts"]
                for request_id in expert["request_ids"]
            )
            self.assertEqual(actual_ids, expected_ids)
            self.assertEqual(
                sum(item["weighted_load_bytes"] for item in replica["experts"]),
                replica["weighted_load_bytes"],
            )
            self.assertGreaterEqual(len(replica["experts"]), 1)

    def test_events_recompute_affected_requests(self) -> None:
        for version, route in self.routes.items():
            events = route["event_results"]
            self.assertEqual(len(events), len(self.projects[version].events))
            self.assertTrue(
                any(event["affected_request_ids"] for event in events)
            )
            self.assertTrue(all(event["feasible_after_event"] for event in events))
            self.assertTrue(
                any(event["shortfall_bytes"] > 0 for event in events)
            )

    def test_rebalance_is_non_worse_and_accounted(self) -> None:
        route = self.routes["v1"]
        before = route["before_rebalance"]["metrics"]["max_replica_load_bytes"]
        after = route["after_rebalance"]["metrics"]["max_replica_load_bytes"]
        self.assertLessEqual(after, before)
        before_sig = {
            row[0]: tuple(row[1:])
            for row in route["before_rebalance"]["assignment_signature"]
        }
        after_sig = {
            row[0]: tuple(row[1:]) for row in route["assignment_signature"]
        }
        changed = {
            request_id
            for request_id in after_sig
            if before_sig.get(request_id) != after_sig[request_id]
        }
        adjustment_ids = {
            adjustment["request_id"]
            for adjustment in route["rebalance_adjustments"]
        }
        self.assertEqual(changed, adjustment_ids)

    def test_reference_module_does_not_import_main_router(self) -> None:
        source = Path("moe_capacity/reference.py").read_text(encoding="utf-8")
        self.assertNotIn("from .router", source)
        self.assertNotIn("import moe_capacity.router", source)


class DeterminismTests(unittest.TestCase):
    def test_generation_and_routing_are_reproducible(self) -> None:
        first = generate_project_set(seed=123, request_count=20)
        second = generate_project_set(seed=123, request_count=20)
        self.assertEqual(
            json.dumps(first, sort_keys=True),
            json.dumps(second, sort_keys=True),
        )
        project_a = normalize_document(first["v1"])
        project_b = normalize_document(second["v1"])
        route_a = run_allocation(project_a)
        route_b = run_allocation(project_b)
        self.assertEqual(
            route_a["assignment_signature"],
            route_b["assignment_signature"],
        )
        self.assertEqual(
            route_a["after_rebalance"]["metrics"],
            route_b["after_rebalance"]["metrics"],
        )

    def test_small_request_count_stays_feasible(self) -> None:
        for count in (1, 2, 4, 8):
            verification = verify_project_set(
                generate_project_set(seed=0, request_count=count)
            )
            self.assertTrue(
                verification["acceptance"]["passed"],
                (count, verification["acceptance"]),
            )


class CliTests(unittest.TestCase):
    def test_demo_cli_writes_reproducible_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary) / "demo"
            command = [
                sys.executable,
                "demo.py",
                "--seed",
                "7",
                "--requests",
                "10",
                "--out",
                str(out),
                "--quiet",
            ]
            result = subprocess.run(
                command,
                cwd=Path(__file__).resolve().parents[1],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            for relative in (
                "schemas/project_v1.json",
                "schemas/project_v2.json",
                "schemas/project_v3.json",
                "normalized/v1.json",
                "routes/v1_route.json",
                "routes/v3_reference_check.json",
                "verification.json",
                "summary.txt",
            ):
                self.assertTrue((out / relative).exists(), relative)
            verification = json.loads(
                (out / "verification.json").read_text(encoding="utf-8")
            )
            self.assertTrue(verification["acceptance"]["passed"])


if __name__ == "__main__":
    unittest.main()

