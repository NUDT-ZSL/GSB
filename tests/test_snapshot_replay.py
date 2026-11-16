import json
import tempfile
import unittest
from pathlib import Path

from drift_workbench.engine.engine import run_convergence
from drift_workbench.engine.errors import ReplayError
from drift_workbench.engine.snapshot import (
    restore_snapshot,
    save_snapshot,
    verify_replay,
    load_snapshot_payload,
)
from drift_workbench.engine.workspace import Workspace

DECL = "drift_workbench/samples/declarations"
ACTUAL = "drift_workbench/samples/actual/snapshot_v1.json"


class SnapshotReplayTests(unittest.TestCase):
    def setUp(self):
        self.workspace = Workspace.from_directories(DECL, ACTUAL)

    def test_save_restore_wholesale_replacement_and_replay(self):
        result = run_convergence(self.workspace, faults={"db-primary": "x"})
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.json"
            save_snapshot(self.workspace, path, result=result)
            restored = Workspace()
            report = restore_snapshot(restored, path)
            self.assertEqual(report["status"], "OK")
            self.assertEqual(
                restored.current_view.group_versions,
                self.workspace.current_view.group_versions,
            )

    def test_artifact_tampering_triggers_e_replay(self):
        run_convergence(self.workspace)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.json"
            save_snapshot(self.workspace, path)
            payload = json.loads(path.read_text(encoding="utf-8"))
            key = sorted(payload["artifacts"])[0]
            payload["artifacts"][key]["artifact_version"] += 5
            broken = Path(tmp) / "broken.json"
            broken.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(ReplayError) as catcher:
                restore_snapshot(Workspace(), broken)
            self.assertEqual(catcher.exception.code, "E_REPLAY")
            self.assertEqual(
                catcher.exception.details["resource_key"], key
            )

    def test_missing_declaration_version_triggers_e_replay(self):
        run_convergence(self.workspace)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.json"
            save_snapshot(self.workspace, path)
            payload = load_snapshot_payload(path)
            network = payload["declaration_history"]["network"]
            network.append(json.loads(json.dumps(network[-1])))
            network[-1]["version"] = 3
            payload["group_versions"]["network"] = 3
            broken = Path(tmp) / "gap.json"
            broken.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(ReplayError) as catcher:
                verify_replay(json.loads(broken.read_text(encoding="utf-8")))
            self.assertEqual(catcher.exception.code, "E_REPLAY")
            self.assertEqual(
                catcher.exception.details["reason"],
                "missing_declaration_version",
            )

    def test_drift_plan_replay_after_faulty_run(self):
        result = run_convergence(
            self.workspace, faults={"db-primary": "timeout"}
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.json"
            save_snapshot(self.workspace, path, result=result)
            report = verify_replay(load_snapshot_payload(path))
            self.assertEqual(report["status"], "OK")
            self.assertEqual(report["plan_count"], len(result.plan_snapshot))


if __name__ == "__main__":
    unittest.main()
