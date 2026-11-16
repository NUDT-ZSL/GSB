import unittest
from dataclasses import replace

from drift_workbench.engine.drift import compute_drifts
from drift_workbench.engine.engine import run_convergence
from drift_workbench.engine.models import STATUS_FAILED, STATUS_SKIPPED, STATUS_SUCCESS
from drift_workbench.engine.workspace import Workspace

DECL = "drift_workbench/samples/declarations"
ACTUAL = "drift_workbench/samples/actual/snapshot_v1.json"


class ConvergenceIsolationTests(unittest.TestCase):
    def setUp(self):
        self.workspace = Workspace.from_directories(DECL, ACTUAL)

    def test_failure_skips_downstream_and_records_codes(self):
        result = run_convergence(
            self.workspace,
            faults={"db-primary": "boom"},
        )
        statuses = {trace.resource_key: trace.status for trace in result.traces}
        self.assertEqual(statuses["db-primary"], STATUS_FAILED)
        self.assertEqual(statuses["svc-order"], STATUS_SKIPPED)
        self.assertEqual(statuses["svc-report"], STATUS_SKIPPED)
        self.assertNotIn("edge-gateway", statuses)
        for trace in result.traces:
            if trace.status in {STATUS_FAILED, STATUS_SKIPPED}:
                self.assertEqual(trace.code, "E_CONVERGE")
        self.assertIn("subnet-app", result.rolled_back)
        self.assertIn("subnet-app", result.succeeded)

    def test_successful_resources_are_not_rolled_back_by_default(self):
        result = run_convergence(
            self.workspace,
            faults={"svc-report": "report failure"},
        )
        success_codes = [
            trace.code
            for trace in result.traces
            if trace.status == STATUS_SUCCESS
        ]
        self.assertTrue(success_codes)
        self.assertTrue(all(code == "OK" for code in success_codes))
        self.assertEqual(result.rolled_back, [])
        self.assertEqual(result.failed, ["svc-report"])

    def test_full_convergence_clears_drift(self):
        result = run_convergence(self.workspace)
        self.assertTrue(all(trace.code == "OK" for trace in result.traces))
        # Explicit recompute reflects the post-convergence actual state.
        self.assertEqual(compute_drifts(self.workspace), [])

    def test_continue_downstream_policy_does_not_skip(self):
        run_convergence(self.workspace)
        latest = self.workspace.declaration_history["data"][-1]
        redis = next(r for r in latest.resources if r.key == "cache-redis")
        changed = replace(
            redis, properties={**redis.properties, "port": 6380}
        )
        document = replace(
            latest,
            version=2,
            resources=tuple(
                changed if r.key == "cache-redis" else r
                for r in latest.resources
            ),
        )
        self.workspace.submit_version(document)
        compute_drifts(self.workspace)
        result = run_convergence(
            self.workspace, faults={"cache-redis": "redis down"}
        )
        self.assertEqual(result.failed, ["cache-redis"])
        self.assertEqual(result.skipped, [])
        self.assertIn("svc-order", result.succeeded)


if __name__ == "__main__":
    unittest.main()
