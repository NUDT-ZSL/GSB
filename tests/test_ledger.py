from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from replica_ledger.analysis import AnalysisResult, analyze_local
from replica_ledger.consensus import determine_truth
from replica_ledger.ledger import LedgerStore, build_ledger_payload
from replica_ledger.planner import build_repair_plan
from replica_ledger.spec import load_replica_group
from replica_ledger.util import atomic_write_json, canonical_json_text, digest_json

from tests.common import SampleWorkspace


class LedgerTests(SampleWorkspace):
    def _payload(self):
        spec = load_replica_group(self.workspace / "scenarios/single/spec.json")
        before = analyze_local(spec, 8)
        after_consensus = determine_truth(spec, before.plan.after_replicas, 8)
        after = AnalysisResult(
            spec=spec,
            consensus=after_consensus,
            plan=build_repair_plan(spec, after_consensus),
        )
        return before, after, build_ledger_payload(before=before, after=after)

    def test_json_sqlite_roundtrip_and_whole_replacement(self) -> None:
        root = Path(self._tempdir.name) / "ledger"
        store = LedgerStore(root / "ledger.json", root / "ledger.sqlite")
        before, after, payload = self._payload()
        view = store.save(payload)
        self.assertEqual(view.status, "saved")
        reopened = store.open(expected_group_state_digest=after.group_state_digest)
        self.assertEqual(reopened.status, "current")
        with sqlite3.connect(root / "ledger.sqlite") as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertTrue(
                {
                    "ledger_current",
                    "replica_distribution",
                    "truth_decisions",
                    "repair_tasks",
                    "repair_costs",
                }.issubset(tables)
            )
            task_count = connection.execute("SELECT COUNT(*) FROM repair_tasks").fetchone()[0]
            self.assertEqual(task_count, len(before.plan.tasks))
        replacement = json.loads(json.dumps(payload))
        replacement["group_state_digest"] = "f" * 64
        replacement["ledger_id"] = "ledger-replacement"
        replaced = store.reconcile(replacement)
        self.assertEqual(replaced.status, "replaced")
        self.assertEqual(len(replaced.backup_paths), 2)
        for path in replaced.backup_paths:
            mode = os.stat(path).st_mode
            self.assertEqual(mode & 0o222, 0)
        stale = store.open(expected_group_state_digest=after.group_state_digest)
        self.assertEqual(stale.status, "stale")
        current = store.open(expected_group_state_digest="f" * 64)
        self.assertEqual(current.status, "current")
        self.assertEqual(current.payload["ledger_id"], "ledger-replacement")

    def test_content_digest_tamper_detected(self) -> None:
        root = Path(self._tempdir.name) / "ledger-tamper"
        store = LedgerStore(root / "ledger.json", root / "ledger.sqlite")
        _, after, payload = self._payload()
        store.save(payload)
        document = json.loads((root / "ledger.json").read_text(encoding="utf-8"))
        document["payload"]["grid_scale"] = 999
        (root / "ledger.json").write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaises(Exception) as context:
            store.open(expected_group_state_digest=after.group_state_digest)
        self.assertEqual(getattr(context.exception, "code", None), "E_LEDGER")

    def test_format_version_mismatch_is_stale_and_replaced(self) -> None:
        root = Path(self._tempdir.name) / "ledger-version"
        store = LedgerStore(root / "ledger.json", root / "ledger.sqlite")
        _, after, payload = self._payload()
        store.save(payload)
        document = json.loads((root / "ledger.json").read_text(encoding="utf-8"))
        old_payload = document["payload"]
        old_payload["ledger_format_version"] = 2
        old_document = {
            "format_version": 2,
            "content_digest": digest_json(old_payload),
            "payload": old_payload,
        }
        atomic_write_json(root / "ledger.json", old_document)
        with sqlite3.connect(root / "ledger.sqlite") as connection:
            connection.execute(
                "UPDATE ledger_current SET format_version=?, content_digest=?, payload_json=? WHERE singleton=1",
                (2, old_document["content_digest"], canonical_json_text(old_payload)),
            )
            connection.commit()
        stale = store.open(
            expected_group_state_digest=after.group_state_digest,
            expected_format_version=1,
        )
        self.assertEqual(stale.status, "stale")
        replaced = store.reconcile(payload)
        self.assertEqual(replaced.status, "replaced")
        self.assertEqual(len(replaced.backup_paths), 2)
        current = store.open(
            expected_group_state_digest=after.group_state_digest,
            expected_format_version=1,
        )
        self.assertEqual(current.status, "current")
