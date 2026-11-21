from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scheduler_diag.service import (
    load_workspace,
    schedule_workspace,
    status_workspace,
    verify_workspace,
)

from tests.helpers import entries_by_pod, sample_copy, write_bundle


class LimitTests(unittest.TestCase):
    def test_unschedulable_pod_gets_filter_chain_attribution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle_data = sample_copy()
            bundle_data["injections"] = []
            bundle_data["pods"] = [bundle_data["pods"][0]]
            for node in bundle_data["nodes"]:
                node["taints"] = []
            bundle_data["pods"][0]["node_affinity"]["required"] = [
                {
                    "key": "zone",
                    "operator": "In",
                    "values": ["does-not-exist"],
                }
            ]
            bundle_dir = write_bundle(root / "bundle", bundle_data)
            database = root / "state.sqlite"
            load_workspace(
                bundle_dir=bundle_dir,
                db_path=database,
                reset=True,
            )
            schedule_workspace(bundle_dir=bundle_dir, db_path=database)
            entry = entries_by_pod(database)["r1-web-control"]
            self.assertEqual(entry["status"], "unschedulable")
            self.assertEqual(entry["plugin"], "FilterChain")
            self.assertEqual(entry["error_code"], "E_NO_FEASIBLE_NODE")
            self.assertEqual(len(entry["eliminations"]), 3)
            for elimination in entry["eliminations"]:
                self.assertEqual(elimination["plugin"], "NodeAffinity")
                self.assertTrue(elimination["source_field"])
            verified = verify_workspace(
                bundle_dir=bundle_dir,
                db_path=database,
            )
            self.assertEqual(verified["checks"]["allocations"], 0)

    def test_plugin_missing_can_be_injected_in_filter_phase(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle_data = sample_copy()
            bundle_data["injections"] = [
                {
                    "id": "inj-missing-filter",
                    "round": 1,
                    "pod": "r1-web-injected",
                    "phase": "filter",
                    "type": "plugin_missing",
                    "node": "node-a",
                    "plugin": "PodAntiAffinity",
                    "message": "filter plugin missing only for this Pod",
                }
            ]
            bundle_dir = write_bundle(root / "bundle", bundle_data)
            database = root / "state.sqlite"
            load_workspace(
                bundle_dir=bundle_dir,
                db_path=database,
                reset=True,
            )
            schedule_workspace(bundle_dir=bundle_dir, db_path=database)
            entry = entries_by_pod(database)["r1-web-injected"]
            self.assertEqual(entry["status"], "injected_filter_failure")
            self.assertEqual(entry["error_code"], "E_INJECT_PLUGIN_MISSING")
            self.assertEqual(entry["plugin"], "PodAntiAffinity")
            self.assertEqual(entry["attribution_node"], "node-a")

    def test_round_time_budget_failure_does_not_debit_resources(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle_data = sample_copy()
            bundle_data["injections"] = []
            bundle_data["config"]["round_time_budget_ms"] = 1
            bundle_dir = write_bundle(root / "bundle", bundle_data)
            database = root / "state.sqlite"
            load_workspace(
                bundle_dir=bundle_dir,
                db_path=database,
                reset=True,
            )
            schedule_workspace(bundle_dir=bundle_dir, db_path=database)
            entries = entries_by_pod(database)
            self.assertTrue(
                all(
                    entry["status"] == "time_budget_failure"
                    and entry["error_code"] == "E_TIME_BUDGET"
                    for entry in entries.values()
                )
            )
            status = status_workspace(
                bundle_dir=bundle_dir,
                db_path=database,
            )
            for node in status["nodes"]:
                self.assertEqual(node["cpu_remaining"], node["cpu_total"])
                self.assertEqual(node["memory_remaining"], node["memory_total"])
            verified = verify_workspace(
                bundle_dir=bundle_dir,
                db_path=database,
            )
            self.assertEqual(verified["checks"]["allocations"], 0)


if __name__ == "__main__":
    unittest.main()
