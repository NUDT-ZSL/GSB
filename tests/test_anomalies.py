from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from cluster_ledger.config import load_inputs
from cluster_ledger.deterministic import compute_aggregate
from cluster_ledger.diagnostics import run_anomaly
from cluster_ledger.deterministic import canonical_json
from cluster_ledger.errors import E_LEDGER, E_PORT, E_VERIFY, E_VERSION
from cluster_ledger.ledger import Ledger, result_checksum
from cluster_ledger.logging_utils import EventLogger
from cluster_ledger.verifier import AuthorityVerifier

from tests.helpers import DATA, config_payload, free_ports, write_config


class AnomalyInjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.dir = Path(self.temp.name)
        self.ports = free_ports(3)
        self.config_path = write_config(
            self.dir / "clusters.json", config_payload(ports=self.ports)
        )
        self.log_path = self.dir / "events.jsonl"
        self.logger = EventLogger(self.log_path)
        self.declaration, self.sample = load_inputs(
            self.config_path, DATA / "sample_data.json"
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _seed_one_result(self, ledger: Ledger, *, declaration=None, sample=None) -> None:
        declaration = declaration or self.declaration
        sample = sample or self.sample
        ledger.reconcile(declaration)
        task = declaration.tasks[0]
        cluster = declaration.clusters[0]
        result = compute_aggregate(
            cluster_id=cluster.cluster_id,
            nodes=cluster.nodes,
            config_version=cluster.config_version,
            config_keys=task.config_keys,
            aggregation={"mode": task.aggregation_mode},
            sample_data=sample,
        )
        ledger.record_result(
            task_id=task.task_id,
            cluster_id=cluster.cluster_id,
            config_version=cluster.config_version,
            aggregation_mode=result.aggregation_mode,
            summary=result.summary,
            components=result.components,
            entry_count=result.entry_count,
            calculation_duration_ms=1,
            dispatch_started_mono_ms=10,
            dispatch_finished_mono_ms=20,
            dispatch_started_mono_ns=10_000_000,
            dispatch_finished_mono_ns=20_000_000,
            declaration_version=declaration.declaration_version,
        )

    def test_port_anomaly_is_e_port_and_logged(self) -> None:
        report = run_anomaly(
            "port",
            config_path=str(self.config_path),
            sample_path=str(DATA / "sample_data.json"),
            ledger_path=str(self.dir / "empty.sqlite3"),
            logger=self.logger,
        )
        self.assertEqual(report["code"], E_PORT)
        events = [json.loads(line) for line in self.log_path.read_text(encoding="utf-8").splitlines()]
        self.assertTrue(any(e.get("code") == E_PORT for e in events))

    def test_version_rollback_is_e_version_and_logged(self) -> None:
        report = run_anomaly(
            "version_rollback",
            config_path=str(self.config_path),
            sample_path=str(DATA / "sample_data.json"),
            ledger_path=str(self.dir / "empty.sqlite3"),
            logger=self.logger,
        )
        self.assertEqual(report["code"], E_VERSION)
        self.assertIn("config_version", report["field_path"])
        events = [json.loads(line) for line in self.log_path.read_text(encoding="utf-8").splitlines()]
        self.assertTrue(any(e.get("code") == E_VERSION for e in events))

    def test_ledger_corruption_is_e_ledger_and_logged(self) -> None:
        ledger_path = self.dir / "ledger.sqlite3"
        ledger = Ledger(ledger_path)
        try:
            self._seed_one_result(ledger)
        finally:
            ledger.close()
        report = run_anomaly(
            "ledger",
            config_path=str(self.config_path),
            sample_path=str(DATA / "sample_data.json"),
            ledger_path=str(ledger_path),
            logger=self.logger,
        )
        self.assertEqual(report["code"], E_LEDGER)
        events = [json.loads(line) for line in self.log_path.read_text(encoding="utf-8").splitlines()]
        self.assertTrue(any(e.get("code") == E_LEDGER for e in events))

    def test_recompute_component_mismatch_is_e_verify_with_first_key(self) -> None:
        ledger_path = self.dir / "ledger-verify.sqlite3"
        ledger = Ledger(ledger_path)
        try:
            self._seed_one_result(ledger)
        finally:
            ledger.close()
        connection = sqlite3.connect(ledger_path)
        connection.row_factory = sqlite3.Row
        try:
            row = dict(connection.execute("SELECT * FROM results LIMIT 1").fetchone())
            components = json.loads(row["components_json"])
            components["app.timeout"] = "sha256:tampered-but-canonical"
            row["components"] = components
            checksum = result_checksum(row)
            connection.execute(
                "UPDATE results SET components_json=?, ledger_checksum=? WHERE id=?",
                (canonical_json(components), checksum, row["id"]),
            )
            connection.commit()
        finally:
            connection.close()

        ledger = Ledger(ledger_path)
        try:
            report = AuthorityVerifier(
                self.declaration, self.sample, ledger, self.logger
            ).verify()
        finally:
            ledger.close()
        self.assertFalse(report["ok"])
        self.assertEqual(report["errors"][0]["code"], E_VERIFY)
        self.assertEqual(report["errors"][0]["first_diff_key"], "app.timeout")
        events = [json.loads(line) for line in self.log_path.read_text(encoding="utf-8").splitlines()]
        self.assertTrue(any(e.get("code") == E_VERIFY for e in events))


if __name__ == "__main__":
    unittest.main()
