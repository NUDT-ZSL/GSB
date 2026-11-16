from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from joinlab.catalog import load_catalog
from joinlab.errors import JoinLabError
from joinlab.injection import create_injection
from joinlab.queries import load_query_file
from joinlab.session import ExperimentSession
from joinlab.snapshot import restore_snapshot, save_snapshot


class SessionSnapshotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog = load_catalog(ROOT / "samples" / "catalog.json")
        cls.queries = load_query_file(ROOT / "samples" / "queries.json", cls.catalog)

    def test_parameter_and_data_versions_move_monotonically(self) -> None:
        session = ExperimentSession(self.catalog)
        initial = session.versions
        after_cost = session.update_cost_params({"nested_loop_pair_cost": 0.4})
        after_data = session.update_sample_scale(0.75)
        self.assertGreater(after_cost.param_version, initial.param_version)
        self.assertEqual(after_cost.data_version, initial.data_version)
        self.assertGreater(after_data.param_version, after_cost.param_version)
        self.assertGreater(after_data.data_version, after_cost.data_version)

    def test_old_version_cannot_be_published(self) -> None:
        session = ExperimentSession(self.catalog)
        old = session.versions
        session.update_cost_params({"scan_row_cost": 1.25})
        session.update_sample_scale(0.8)
        with self.assertRaises(JoinLabError) as raised:
            session.evaluate(self.queries[0].sql, expected_versions=old)
        self.assertEqual(raised.exception.code, "E_STALE")
        current = session.evaluate(self.queries[0].sql, expected_versions=session.versions)
        self.assertEqual(current.versions, session.versions)
        self.assertEqual(len(session.history), 1)

    def test_snapshot_restore_is_full_replacement_and_replays(self) -> None:
        session = ExperimentSession(self.catalog)
        original = [session.evaluate(item.sql, name=item.name) for item in self.queries]
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "snapshot.json"
            save_snapshot(session, path)
            session.update_cost_params({"nested_loop_pair_cost": 0.99})
            session.update_sample_scale(0.5)
            self.assertNotEqual(session.versions, original[0].versions)
            restore = restore_snapshot(path, current_session=session)
            self.assertEqual(restore.mode, "full_replace")
            self.assertFalse(restore.version_comparison["same_versions"])
            self.assertEqual(restore.replayed_queries, len(self.queries))
            replay = {entry.query.query_id: entry for entry in restore.session.latest_evaluations()}
            for before in original:
                after = replay[before.query.query_id]
                self.assertEqual(after.selected_plan.plan_id, before.selected_plan.plan_id)
                self.assertEqual(after.execution.selected.digest, before.execution.selected.digest)
                self.assertEqual(
                    after.execution.selected.stats_fingerprint,
                    before.execution.selected.stats_fingerprint,
                )

    def test_snapshot_omits_stale_results_and_replays_current_version(self) -> None:
        session = ExperimentSession(self.catalog)
        session.evaluate(self.queries[0].sql, name=self.queries[0].name)
        session.update_cost_params({"nested_loop_pair_cost": 0.01})
        current = session.evaluate(self.queries[0].sql, name=self.queries[0].name)
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "snapshot.json"
            saved = save_snapshot(session, path)
            self.assertEqual(saved["history_count"], 1)
            self.assertGreaterEqual(saved["stale_history_omitted"], 1)
            restore = restore_snapshot(path)
            replayed = restore.session.latest_evaluations()[0]
            self.assertEqual(replayed.selected_plan.plan_id, current.selected_plan.plan_id)
            self.assertEqual(replayed.execution.selected.digest, current.execution.selected.digest)

    def test_snapshot_tamper_returns_e_snap_with_differences(self) -> None:
        session = ExperimentSession(self.catalog)
        session.evaluate(self.queries[0].sql, name=self.queries[0].name)
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "snapshot.json"
            save_snapshot(session, path)
            document = json.loads(path.read_text(encoding="utf-8"))
            document["history"][0]["execution"]["result_digest"] = "tampered"
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaises(JoinLabError) as raised:
                restore_snapshot(path)
            self.assertEqual(raised.exception.code, "E_SNAP")
            differences = raised.exception.details["differences"]
            self.assertEqual(differences[0]["field"], "result_digest")

    def test_missing_parameter_version_returns_e_snap(self) -> None:
        session = ExperimentSession(self.catalog)
        session.evaluate(self.queries[0].sql)
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "snapshot.json"
            save_snapshot(session, path)
            document = json.loads(path.read_text(encoding="utf-8"))
            del document["versions"]["param_version"]
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaises(JoinLabError) as raised:
                restore_snapshot(path)
            self.assertEqual(raised.exception.code, "E_SNAP")
            self.assertEqual(raised.exception.path, "$.versions.param_version")

    def test_all_three_injections_are_file_only_and_expected(self) -> None:
        expected = {"skew": "E_SKEW", "type": "E_TYPE", "snapshot-version": "E_SNAP"}
        with tempfile.TemporaryDirectory() as temp_dir:
            for kind, code in expected.items():
                with self.subTest(kind=kind):
                    artifact = create_injection(kind, ROOT / "samples", Path(temp_dir) / kind)
                    self.assertEqual(artifact.detected_code, code)
                    self.assertTrue(artifact.to_dict()["input_only"])
                    self.assertTrue(Path(artifact.output_dir).is_dir())


if __name__ == "__main__":
    unittest.main()
