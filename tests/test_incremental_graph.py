import unittest
from dataclasses import replace

from drift_workbench.engine.errors import CycleError, ReferenceError
from drift_workbench.engine.workspace import Workspace
from drift_workbench.engine.drift import build_plan, compute_drifts
from drift_workbench.engine.engine import run_convergence

DECL = "drift_workbench/samples/declarations"
ACTUAL = "drift_workbench/samples/actual/snapshot_v1.json"


class IncrementalGraphTests(unittest.TestCase):
    def setUp(self):
        self.workspace = Workspace.from_directories(DECL, ACTUAL)

    def test_incremental_artifact_versions_are_immutable(self):
        run_convergence(self.workspace)
        before = {
            key: artifact.artifact_version
            for key, artifact in self.workspace.current_view.artifacts.items()
        }
        latest = self.workspace.declaration_history["network"][-1]
        subnet = next(r for r in latest.resources if r.key == "subnet-app")
        changed = replace(
            subnet, properties={**subnet.properties, "cidr": "10.0.20.0/24"}
        )
        document = replace(
            latest,
            version=2,
            resources=tuple(
                changed if r.key == "subnet-app" else r
                for r in latest.resources
            ),
        )
        self.workspace.submit_version(document)
        after = {
            key: artifact.artifact_version
            for key, artifact in self.workspace.current_view.artifacts.items()
        }
        self.assertEqual(after["subnet-app"], before["subnet-app"] + 1)
        unchanged = set(before) - {"subnet-app"}
        for key in unchanged:
            self.assertEqual(after[key], before[key], key)
        self.assertEqual(
            set(self.workspace.current_view.invalidated_keys),
            {"cache-redis", "db-primary", "svc-order", "svc-report", "edge-gateway"},
        )
        self.assertGreaterEqual(len(self.workspace.views), 2)

    def test_delete_referenced_resource_returns_e_ref(self):
        with self.assertRaises(ReferenceError) as catcher:
            self.workspace.delete_resource("subnet-app")
        self.assertEqual(catcher.exception.code, "E_REF")
        self.assertEqual(
            catcher.exception.details["referrers"],
            ["cache-redis", "db-primary"],
        )

    def test_cycle_detection(self):
        with self.assertRaises(CycleError) as catcher:
            Workspace.from_directories(
                "drift_workbench/samples/invalid/decl_cycle",
                ACTUAL,
            )
        self.assertIn("cycle", catcher.exception.details)

    def test_topological_plan_order_is_stable(self):
        drifts = compute_drifts(self.workspace)
        plan = build_plan(self.workspace)
        keys = [action.resource_key for action in plan]
        self.assertEqual(keys, sorted(keys, key=lambda k: keys.index(k)))
        self.assertIn("subnet-app", keys)
        self.assertEqual(keys[-1], "legacy-monitor")
        self.assertEqual(len(drifts), 5)


if __name__ == "__main__":
    unittest.main()
