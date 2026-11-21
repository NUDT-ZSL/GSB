from __future__ import annotations

import unittest

from quant_diag.web import WorkbenchService

from tests.helpers import Workspace


class WorkbenchServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace = Workspace()
        self.service = WorkbenchService(
            self.workspace.root, self.workspace.root / "runtime_web"
        )

    def tearDown(self) -> None:
        self.workspace.cleanup()

    def test_service_runs_and_exposes_spectrum_and_selfcheck(self) -> None:
        initial = self.service.overview()
        self.assertEqual(initial["view"], {})
        result = self.service.action({"action": "run"})
        self.assertTrue(result["ok"])
        view = result["overview"]["view"]
        self.assertEqual(view["version"], 4)
        self.assertEqual(len(view["diagnostics"]["layers"]), 4)
        self.assertTrue(view["self_check"]["passed"])
        self.assertEqual(len(view["history"]), 4)

    def test_service_injection_reports_business_code(self) -> None:
        result = self.service.action(
            {
                "action": "inject",
                "kind": "weight_truncate",
                "layer_id": "layer_1",
            }
        )
        self.assertTrue(result["ok"])
        self.assertTrue(result["result"]["raised"])
        self.assertEqual(result["result"]["error"]["code"], "E_WEIGHT_TRUNCATED")
        self.assertEqual(result["result"]["error"]["layer_id"], "layer_1")

    def test_service_recovery_and_revision_update_actions(self) -> None:
        recovery = self.service.action({"action": "recover"})
        self.assertTrue(recovery["ok"])
        self.assertEqual(recovery["comparison"]["kill_exit_code"], 137)
        self.assertTrue(recovery["comparison"]["byte_equivalent_result"])
        self.assertEqual(
            recovery["comparison"]["recovery_report"]["resume_layer_index"], 0
        )
        update = self.service.action({"action": "update"})
        self.assertTrue(update["ok"])
        self.assertEqual(
            update["comparison"]["affected_layers"],
            ["layer_1", "layer_2", "layer_3"],
        )
        self.assertEqual(update["comparison"]["reused_layers"], ["layer_0"])
        self.assertTrue(update["overview"]["view"]["self_check"]["passed"])


if __name__ == "__main__":
    unittest.main()
