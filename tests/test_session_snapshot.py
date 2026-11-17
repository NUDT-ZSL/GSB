from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from infer_workbench.assets import generate_assets, load_assets
from infer_workbench.config import BatchConfig
from infer_workbench.errors import WorkbenchError
from infer_workbench.session import InferenceSession
from infer_workbench.snapshots import load_snapshot, save_snapshot, seal_snapshot


ROOT = Path(__file__).resolve().parent.parent


def _config(**overrides):
    raw = {
        "batch_size": 3,
        "sort_strategy": "input_norm",
        "precision_mode": "fixed",
        "quant_scale": 1000,
        "drift_threshold": 0.005,
    }
    raw.update(overrides)
    return BatchConfig.from_dict(raw)


class SessionSnapshotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.assets = load_assets(ROOT / "data")

    def test_version_change_replaces_all_batches_and_reports_diff(self) -> None:
        q3 = _config()
        q2 = _config(quant_scale=100)
        with tempfile.TemporaryDirectory() as temporary:
            session = InferenceSession(self.assets, snapshot_dir=temporary)
            session.run(q3)
            report = session.run(q2)
        self.assertEqual(len(report["batches"]), 4)
        self.assertTrue(all(batch["config_version"] == q2.config_version for batch in report["batches"]))
        self.assertFalse(any(batch["config_version"] == q3.config_version for batch in report["batches"]))
        self.assertTrue(any(item["config_version"] == q3.config_version for item in report["stale_versions"]))
        transition = report["version_transition"]
        self.assertEqual(transition["old_config_version"], q3.config_version)
        self.assertEqual(transition["new_config_version"], q2.config_version)
        self.assertEqual(transition["changed_parameters"], ["quant_scale", "quant_scale_power"])
        self.assertEqual(transition["changed_batch_indices"], [1, 2, 3, 4])
        self.assertEqual(transition["changed_layers"], ["hidden_1", "hidden_2", "output"])
        self.assertEqual(transition["drift_direction"], "higher")

    def test_snapshot_restore_replaces_state_keeps_readonly_backup_and_replays(self) -> None:
        q3 = _config()
        q2 = _config(quant_scale=100)
        with tempfile.TemporaryDirectory() as temporary:
            snapshot_dir = Path(temporary) / "snapshots"
            snapshot_dir.mkdir()
            snapshot_path = snapshot_dir / "partial.json"
            session = InferenceSession(self.assets, snapshot_dir=snapshot_dir)
            session.run(q3, stop_after=2, snapshot_path=snapshot_path)
            snapshot_before = load_snapshot(snapshot_path)
            self.assertEqual(len(snapshot_before["completed_batches"]), 2)
            self.assertEqual(snapshot_before["next_batch_index"], 3)

            q2_report = session.run(q2)
            self.assertEqual(q2_report["config_version"], q2.config_version)
            restore = session.restore_snapshot(snapshot_path)
            self.assertIsNone(restore["code"])
            self.assertTrue(restore["replaced_state_wholesale"])
            self.assertTrue(restore["config_version_changed"])
            self.assertEqual(restore["loaded_completed_batches"], 2)
            self.assertEqual(session.config_version, q3.config_version)
            self.assertEqual(len(session.batches), 2)
            self.assertTrue(all(batch["config_version"] == q3.config_version for batch in session.batches))
            backup = Path(temporary) / restore["readonly_backup"]
            self.assertTrue(backup.exists())
            self.assertEqual(os.stat(backup).st_mode & 0o222, 0)

            replay = session.replay_snapshot(snapshot_path, expected_config=q3)
            self.assertEqual(replay["status"], "verified")
            self.assertIsNone(replay["code"])
            self.assertEqual(replay["checked_batches"], 2)
            self.assertEqual(replay["checked_samples"], 6)
            self.assertIsNone(replay["first_mismatch"])

            report = session.continue_remaining(snapshot_path=snapshot_path)
            self.assertTrue(report["state"]["complete"])
            self.assertEqual(len(report["batches"]), 4)
            replay_after = session.replay_snapshot(snapshot_path, expected_config=q3)
            self.assertEqual(replay_after["checked_batches"], 4)
            self.assertIsNone(replay_after["code"])
            os.chmod(backup, 0o644)

    def test_replay_localizes_first_injected_mismatch(self) -> None:
        config = _config()
        with tempfile.TemporaryDirectory() as temporary:
            snapshot_dir = Path(temporary)
            snapshot_path = snapshot_dir / "partial.json"
            session = InferenceSession(self.assets, snapshot_dir=snapshot_dir)
            session.run(config, stop_after=2, snapshot_path=snapshot_path)
            payload = load_snapshot(snapshot_path)
            payload["completed_batches"][1]["samples"][0]["diagnostics"]["layers"][0][
                "cumulative_error_bound_grid"
            ] += 1
            save_snapshot(snapshot_path, seal_snapshot(payload))
            replay = session.replay_snapshot(snapshot_path, expected_config=config)
        self.assertEqual(replay["code"], "E_REPLAY")
        self.assertEqual(replay["first_mismatch"]["batch_index"], 2)
        self.assertEqual(replay["first_mismatch"]["sample_id"], payload["completed_batches"][1]["samples"][0]["sample_id"])
        self.assertEqual(replay["first_mismatch"]["layer"], "hidden_1")
        self.assertEqual(replay["first_mismatch"]["field"], "cumulative_error_bound_grid")

    def test_config_or_quant_mismatch_is_reported_as_unreproducible(self) -> None:
        q3 = _config()
        q2 = _config(quant_scale=100)
        with tempfile.TemporaryDirectory() as temporary:
            snapshot_path = Path(temporary) / "snapshot.json"
            session = InferenceSession(self.assets, snapshot_dir=temporary)
            session.run(q3, stop_after=1, snapshot_path=snapshot_path)
            replay = session.replay_snapshot(snapshot_path, expected_config=q2)
        self.assertEqual(replay["code"], "E_REPLAY")
        items = {item["item"]: item for item in replay["unreproducible_items"]}
        self.assertIn("config_version", items)
        self.assertIn("quant_scale", items)
        self.assertEqual(items["quant_scale"]["snapshot"], 1000)
        self.assertEqual(items["quant_scale"]["current"], 100)

    def test_corrupted_snapshot_returns_e_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            snapshot_path = Path(temporary) / "snapshot.json"
            session = InferenceSession(self.assets, snapshot_dir=temporary)
            session.run(_config(), stop_after=1, snapshot_path=snapshot_path)
            payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
            payload["completed_batches"][0]["sample_ids"][0] = "BROKEN"
            snapshot_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
            with self.assertRaises(WorkbenchError) as raised:
                session.restore_snapshot(snapshot_path)
        self.assertEqual(raised.exception.code, "E_SNAPSHOT")

    def test_restore_with_different_assets_clears_state_with_e_data(self) -> None:
        q3 = _config()
        with tempfile.TemporaryDirectory() as temporary:
            snapshot_path = Path(temporary) / "snapshot.json"
            session = InferenceSession(self.assets, snapshot_dir=temporary)
            session.run(q3, stop_after=1, snapshot_path=snapshot_path)
            other_assets = generate_assets(
                seed=20261118,
                sample_count=12,
                input_dim=4,
                hidden_dims=(6, 5),
                output_dim=3,
            )
            other_session = InferenceSession(other_assets, snapshot_dir=temporary)
            other_session.run(q3, stop_after=1)
            restore = other_session.restore_snapshot(snapshot_path)
            self.assertEqual(restore["code"], "E_DATA")
            self.assertEqual(len(other_session.batches), 0)
            self.assertEqual(other_session.status, "incompatible")
            backup = other_session.snapshot_dir.parent / restore["readonly_backup"]
            os.chmod(backup, 0o644)


if __name__ == "__main__":
    unittest.main()
