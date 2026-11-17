from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from model_service_workbench.declaration import load_declaration, load_declaration_text
from model_service_workbench.errors import DeclarationError
from model_service_workbench.workbench import Workbench


ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "samples" / "cluster.json"
MODIFIED = ROOT / "samples" / "cluster_modified.json"


class DeclarationValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base = json.loads(SAMPLE.read_text(encoding="utf-8"))

    def _load_mutated(self, mutate) -> None:
        document = copy.deepcopy(self.base)
        mutate(document)
        load_declaration_text(
            json.dumps(document, ensure_ascii=False, indent=2),
            "memory.json",
        )

    def _assert_decl_error(self, mutate, expected_path: str) -> None:
        with self.assertRaises(DeclarationError) as context:
            self._load_mutated(mutate)
        self.assertEqual(context.exception.code, "E_DECL")
        self.assertEqual(context.exception.path, expected_path)

    def test_valid_sample_and_sorted_services(self) -> None:
        declaration = load_declaration(SAMPLE)
        self.assertEqual(declaration.declaration_version, 1)
        self.assertEqual(declaration.total_minutes, 8)
        self.assertEqual(
            declaration.service_names,
            ("alpha", "beta", "gamma"),
        )

    def test_duplicate_service_name_is_e_decl(self) -> None:
        def mutate(document):
            document["services"][1]["name"] = "alpha"

        self._assert_decl_error(mutate, "$.services[1].name")

    def test_negative_request_is_e_decl(self) -> None:
        def mutate(document):
            document["services"][0]["requests"][3] = -1

        self._assert_decl_error(mutate, "$.services[0].requests[3]")

    def test_non_integer_request_is_e_decl(self) -> None:
        def mutate(document):
            document["services"][2]["requests"][0] = 1.5

        self._assert_decl_error(mutate, "$.services[2].requests[0]")

    def test_zero_global_budget_is_e_decl(self) -> None:
        def mutate(document):
            document["global_replica_budget"] = 0

        self._assert_decl_error(mutate, "$.global_replica_budget")

    def test_zero_concurrency_is_e_decl(self) -> None:
        def mutate(document):
            document["services"][0]["concurrency_per_replica"] = 0

        self._assert_decl_error(mutate, "$.services[0].concurrency_per_replica")

    def test_zero_latency_is_e_decl(self) -> None:
        def mutate(document):
            document["services"][1]["request_latency_ms"] = 0

        self._assert_decl_error(mutate, "$.services[1].request_latency_ms")

    def test_min_greater_than_max_is_e_decl(self) -> None:
        def mutate(document):
            document["services"][2]["min_replicas"] = 5

        self._assert_decl_error(mutate, "$.services[2].min_replicas")

    def test_invalid_json_is_e_decl(self) -> None:
        with self.assertRaises(DeclarationError) as context:
            load_declaration_text("{not-json", "bad.json")
        self.assertEqual(context.exception.code, "E_DECL")
        self.assertEqual(context.exception.path, "$")

    def test_missing_field_is_e_decl_with_path(self) -> None:
        def mutate(document):
            document["services"][0].pop("concurrency_quota")

        with self.assertRaises(DeclarationError) as context:
            self._load_mutated(mutate)
        self.assertEqual(context.exception.code, "E_DECL")
        self.assertEqual(context.exception.path, "$.services[0].concurrency_quota")


class VersionEvolutionTests(unittest.TestCase):
    def test_version_monotonic_and_old_conclusions_stale(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workbench = Workbench(
                SAMPLE,
                root / "ledger.sqlite",
                root / "snapshots",
            )
            try:
                first = workbench.load_declaration()
                self.assertEqual(first.declaration.declaration_version, 1)
                first_run = workbench.run_all()["run_id"]
                self.assertEqual(len(workbench.get_current_view()["minutes"]), 8)

                second = workbench.load_declaration(MODIFIED)
                self.assertTrue(second.version_changed)
                self.assertEqual(second.previous_version, 1)
                self.assertEqual(second.declaration.declaration_version, 2)
                self.assertEqual(second.affected_services, ("beta",))
                self.assertEqual(second.affected_minute_range, (0, 7))
                self.assertIn(
                    '$.services["beta"].concurrency_per_replica',
                    [item["path"] for item in second.differences],
                )
                old_run = workbench.ledger.get_run(first_run)
                self.assertTrue(old_run["is_stale"])
                self.assertFalse(old_run["is_current"])
                self.assertIsNone(workbench.get_current_view()["run"])

                second_run = workbench.run_all()["run_id"]
                view = workbench.get_current_view()
                self.assertEqual(view["run"]["run_id"], second_run)
                self.assertEqual(view["run"]["declaration_version"], 2)
                self.assertEqual(len(view["minutes"]), 8)
                self.assertEqual(
                    len({minute["minute"] for minute in view["minutes"]}),
                    8,
                )

                third = workbench.load_declaration(MODIFIED)
                self.assertFalse(third.version_changed)
                self.assertEqual(third.declaration.declaration_version, 2)

                reverted = workbench.load_declaration(SAMPLE)
                self.assertTrue(reverted.version_changed)
                self.assertEqual(reverted.previous_version, 2)
                self.assertEqual(reverted.declaration.declaration_version, 3)
                reloaded = workbench.load_declaration(SAMPLE)
                self.assertFalse(reloaded.version_changed)
                self.assertEqual(reloaded.declaration.declaration_version, 3)
            finally:
                workbench.close()


if __name__ == "__main__":
    unittest.main()
