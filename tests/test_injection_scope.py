from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scheduler_diag.service import (
    inject_workspace,
    load_workspace,
    schedule_workspace,
    status_workspace,
    verify_workspace,
)

from tests.helpers import SAMPLE_DIR, entries_by_pod


class InjectionScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = self.root / "state.sqlite"
        load_workspace(
            bundle_dir=SAMPLE_DIR,
            db_path=self.database,
            reset=True,
        )
        schedule_workspace(
            bundle_dir=SAMPLE_DIR,
            db_path=self.database,
        )
        self.entries = entries_by_pod(self.database)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_each_injection_has_exact_scope_and_error_code(self) -> None:
        expected = {
            "r1-web-injected": (
                "E_INJECT_NODE_LABEL_CONFLICT",
                "NodeAffinity",
                "node-a",
                "filter",
            ),
            "r2-oversell-injected": (
                "E_INJECT_RESOURCE_OVERCOMMIT",
                "ResourceFit",
                "node-c",
                "filter",
            ),
            "r3-weight-injected": (
                "E_INJECT_INVALID_WEIGHT",
                "ResourceBinPack",
                "node-a",
                "score",
            ),
            "r4-plugin-injected": (
                "E_INJECT_PLUGIN_MISSING",
                "NodeAffinityMatch",
                "node-c",
                "score",
            ),
        }
        for pod_name, (code, plugin, node, phase) in expected.items():
            entry = self.entries[pod_name]
            self.assertEqual(entry["status"], f"injected_{phase}_failure")
            self.assertEqual(entry["error_code"], code)
            self.assertEqual(entry["plugin"], plugin)
            self.assertIsNone(entry["selected_node"])
            self.assertEqual(len(entry["injection_consumed"]), 1)
            injection = entry["injection"]
            self.assertEqual(injection["round"], entry["round"])
            self.assertEqual(injection["pod"], pod_name)
            self.assertEqual(injection["node"], node)
            self.assertEqual(injection["plugin"], plugin)
            elimination = entry["eliminations"][0]
            self.assertEqual(elimination["plugin"], plugin)
            self.assertEqual(elimination["node"], node)
            self.assertEqual(elimination["error_code"], code)
            self.assertTrue(elimination["source_field"].startswith("injections["))

    def test_controls_in_same_rounds_still_bind(self) -> None:
        controls = [
            "r1-web-control",
            "r2-gpu-control",
            "r3-api-control",
            "r4-cache-control",
        ]
        for pod_name in controls:
            self.assertEqual(self.entries[pod_name]["status"], "bound")
            self.assertIsNotNone(self.entries[pod_name]["selected_node"])
            self.assertEqual(self.entries[pod_name]["injection_consumed"], [])

    def test_injection_consumption_and_verify(self) -> None:
        result = verify_workspace(
            bundle_dir=SAMPLE_DIR,
            db_path=self.database,
        )
        self.assertEqual(result["checks"]["injection_watermark"], 4)
        self.assertEqual(result["checks"]["duplicate_debits"], 0)
        self.assertEqual(result["checks"]["dangling_debits"], 0)
        status = inject_workspace(
            bundle_dir=SAMPLE_DIR,
            db_path=self.database,
        )
        self.assertEqual(status["injection_watermark"], 4)
        self.assertTrue(all(item["consumed"] for item in status["injections"]))
        self.assertEqual(
            [item["consumed_order"] for item in status["injections"]],
            [1, 2, 3, 4],
        )

    def test_resource_ledger_only_debits_bound_controls(self) -> None:
        status = status_workspace(
            bundle_dir=SAMPLE_DIR,
            db_path=self.database,
        )
        remaining = {node["name"]: node for node in status["nodes"]}
        self.assertEqual(remaining["node-a"]["cpu_remaining"], 3500)
        self.assertEqual(remaining["node-a"]["memory_remaining"], 7680)
        self.assertEqual(remaining["node-b"]["cpu_remaining"], 2300)
        self.assertEqual(remaining["node-b"]["memory_remaining"], 5120)
        self.assertEqual(remaining["node-c"]["cpu_remaining"], 3000)
        self.assertEqual(remaining["node-c"]["memory_remaining"], 5888)


if __name__ == "__main__":
    unittest.main()
