from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from lease_arbiter.engine import ArbiterEngine, RenewRequest
from lease_arbiter.errors import BizError, Code
from lease_arbiter.ledger import Ledger, canonical_json
from lease_arbiter.validation import load_bundle
from lease_arbiter.verify import Verification

from tests.helpers import sample_paths, write_docs


class EngineSampleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.paths = sample_paths(self.root / "data")
        bundle = load_bundle(
            self.paths["accelerator_cards.json"],
            self.paths["jobs.json"],
            self.paths["lease_config.json"],
            self.paths["anomaly_injections.json"],
        )
        self.ledger = Ledger(
            self.root / "state" / "ledger.db",
            self.root / "state" / "pointer.json",
            self.root / "state" / "reports",
        )
        with self.ledger.lock():
            self.ledger.load_bundle(bundle, force=True)
        self.engine = ArbiterEngine(self.ledger)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _run_to_round_9(self) -> dict[int, dict]:
        reports = {1: self.engine.run_lease_round(1)}
        reports[2] = self.engine.run_lease_round(2)
        reports[3] = self.engine.run_lease_round(3)
        preempt = self.engine.preempt_round(3, "job-900", apply=True)
        job_100_lease = next(
            grant["lease_id"]
            for grant in reports[1]["grants"]
            if grant["job_id"] == "job-100"
        )
        reports[4] = self.engine.run_lease_round(
            4,
            [
                RenewRequest(
                    lease_id=job_100_lease,
                    job_id="job-100",
                    generation=1,
                )
            ],
        )
        for round_no in (5, 6, 7, 8, 9):
            reports[round_no] = self.engine.run_lease_round(round_no)
        reports[100] = preempt
        return reports

    def test_round_idempotence(self) -> None:
        first = self.engine.run_lease_round(1)
        second = self.engine.run_lease_round(1)
        self.assertEqual(first, second)
        self.assertEqual(first["ledger_version"], 1)
        connection = self.ledger.connect()
        try:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM rounds").fetchone()[0], 1
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM leases").fetchone()[0],
                len(first["grants"]),
            )
        finally:
            connection.close()

    def test_standalone_renewal_after_round_commit(self) -> None:
        first = self.engine.run_lease_round(1)
        self.engine.run_lease_round(2)
        job_100_lease = next(
            grant["lease_id"]
            for grant in first["grants"]
            if grant["job_id"] == "job-100"
        )
        renewed = self.engine.renew_round(
            2,
            RenewRequest(
                lease_id=job_100_lease,
                job_id="job-100",
                generation=1,
            ),
        )
        self.assertEqual(renewed["operation"], "renew")
        self.assertEqual(renewed["renewals"][0]["generation_after"], 2)
        self.assertEqual(renewed["ledger_version"], 3)
        verdict = Verification(self.ledger).run()
        self.assertTrue(verdict["ok"], json.dumps(verdict, indent=2))

    def test_two_independent_runs_are_byte_identical(self) -> None:
        bundle = load_bundle(
            self.paths["accelerator_cards.json"],
            self.paths["jobs.json"],
            self.paths["lease_config.json"],
            self.paths["anomaly_injections.json"],
        )
        roots = [self.root / "det-a", self.root / "det-b"]
        reports_by_run: list[list[dict]] = []
        for root in roots:
            ledger = Ledger(
                root / "ledger.db", root / "pointer.json", root / "reports"
            )
            with ledger.lock():
                ledger.load_bundle(bundle, force=True)
            engine = ArbiterEngine(ledger)
            reports = [engine.run_lease_round(1), engine.run_lease_round(2)]
            reports.append(engine.run_lease_round(3))
            reports.append(engine.preempt_round(3, "job-900", apply=True))
            job_100_lease = next(
                grant["lease_id"]
                for grant in reports[0]["grants"]
                if grant["job_id"] == "job-100"
            )
            reports.append(
                engine.run_lease_round(
                    4,
                    [
                        RenewRequest(
                            lease_id=job_100_lease,
                            job_id="job-100",
                            generation=1,
                        )
                    ],
                )
            )
            for round_no in range(5, 10):
                reports.append(engine.run_lease_round(round_no))
            job_900_lease = reports[3]["grants"][0]["lease_id"]
            reports.append(
                engine.run_lease_round(
                    10,
                    [
                        RenewRequest(
                            lease_id=job_900_lease,
                            job_id="job-900",
                            generation=1,
                        )
                    ],
                )
            )
            reports_by_run.append(reports)
        left = [canonical_json(report) for report in reports_by_run[0]]
        right = [canonical_json(report) for report in reports_by_run[1]]
        self.assertEqual(left, right)

    def test_preemption_chain_is_minimal_and_refund_is_pending(self) -> None:
        reports = self._run_to_round_9()
        preempt = reports[100]
        self.assertEqual(
            [item["job_id"] for item in preempt["selection_chain"]], ["job-700"]
        )
        self.assertEqual(
            [item["job_id"] for item in preempt["refund_records"]], ["job-700"]
        )
        self.assertEqual(preempt["refund_records"][0]["status"], "pending")
        self.assertEqual(preempt["grants"][0]["job_id"], "job-900")
        self.assertTrue(preempt["reclaim_summary"])
        repeated = self.engine.preempt_round(3, "job-900", apply=True)
        self.assertEqual(repeated, preempt)
        connection = self.ledger.connect()
        try:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM refunds").fetchone()[0], 1
            )
        finally:
            connection.close()

    def test_renewal_then_expiry_is_clock_deterministic(self) -> None:
        reports = self._run_to_round_9()
        renewal = reports[4]["renewals"][0]
        self.assertEqual(renewal["generation_before"], 1)
        self.assertEqual(renewal["generation_after"], 2)
        self.assertEqual(renewal["new_expires_at_ms"], 27000)
        expired_jobs = {
            item["job_id"] for item in reports[8]["expirations"]
        }
        self.assertIn("job-100", expired_jobs)

    def test_fragmentation_is_not_misreported_as_true_no_capacity(self) -> None:
        reports = self._run_to_round_9()
        denial = reports[8]["denials"][0]
        self.assertEqual(denial["job_id"], "job-1100")
        self.assertEqual(denial["fragment_classification"], "fragmentation")
        self.assertEqual(
            denial["first_constraint"]["constraint"], "fragmentation"
        )
        self.assertIn(
            denial["first_constraint"]["card_id"], {"card-b", "card-c"}
        )
        analysis = next(
            item
            for item in reports[8]["fragment_analyses"]
            if item["job_id"] == "job-1100"
        )
        fragmented = [
            item
            for item in analysis["cards"]
            if item["classification"] == "fragmentation"
        ]
        self.assertGreaterEqual(len(fragmented), 1)
        for card in fragmented:
            self.assertGreaterEqual(card["free_totals"]["compute"], 48)
            self.assertLess(card["longest_contiguous"]["compute"], 48)
            self.assertGreaterEqual(len(card["compute_fragments"]), 2)

    def test_anomalies_are_isolated_and_mapped_to_distinct_codes(self) -> None:
        reports = self._run_to_round_9()
        job_900_lease = reports[100]["grants"][0]["lease_id"]
        round_10 = self.engine.run_lease_round(
            10,
            [
                RenewRequest(
                    lease_id=job_900_lease,
                    job_id="job-900",
                    generation=1,
                )
            ],
        )
        codes = {item["code"] for item in round_10["anomalies"]}
        self.assertEqual(
            codes,
            {
                Code.ANOMALY_INTERVAL_OVERLAP,
                Code.ANOMALY_SHARE_NONCONSERVATION,
                Code.ANOMALY_GENERATION_REGRESSION,
                Code.ANOMALY_FRAGMENT_DIGEST,
            },
        )
        self.assertEqual(
            {grant["job_id"] for grant in round_10["grants"]}, {"job-1201"}
        )
        self.assertEqual(round_10["denials"][1]["job_id"], "job-1200")
        verdict = Verification(self.ledger).run()
        self.assertTrue(verdict["ok"], json.dumps(verdict, indent=2))

    def test_clock_rollback_beyond_tolerance_is_rejected(self) -> None:
        self.engine.run_lease_round(3)
        with self.assertRaises(BizError) as context:
            self.engine.run_lease_round(1)
        self.assertEqual(context.exception.code, Code.CLOCK_ROLLBACK)
        connection = self.ledger.connect()
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT value FROM meta WHERE key = 'ledger_version'"
                ).fetchone()[0],
                "1",
            )
        finally:
            connection.close()


class CustomAllocationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _engine(self, documents: dict) -> ArbiterEngine:
        paths = write_docs(self.root / "data", documents)
        bundle = load_bundle(
            paths["accelerator_cards.json"],
            paths["jobs.json"],
            paths["lease_config.json"],
            paths["anomaly_injections.json"],
        )
        ledger = Ledger(
            self.root / "state" / "ledger.db",
            self.root / "state" / "pointer.json",
            self.root / "state" / "reports",
        )
        with ledger.lock():
            ledger.load_bundle(bundle, force=True)
        return ArbiterEngine(ledger)

    def _base(self) -> dict:
        return {
            "accelerator_cards.json": {
                "schema_version": 1,
                "cards": [
                    {
                        "card_id": "a",
                        "total_compute_shares": 16,
                        "total_memory_shares": 8,
                        "granularity": 4,
                        "health": "healthy",
                        "topology_domain": "d",
                    },
                    {
                        "card_id": "b",
                        "total_compute_shares": 16,
                        "total_memory_shares": 8,
                        "granularity": 4,
                        "health": "healthy",
                        "topology_domain": "d",
                    },
                ],
            },
            "lease_config.json": {
                "schema_version": 1,
                "quota_group_compute_limits": {"g": 64},
                "alignment_granularity": 4,
                "renewal_grace_ms": 100,
                "fragmentation_threshold": 0.25,
                "clock_steps_ms": [1000, 2000],
                "clock_rollback_tolerance_ms": 0,
            },
            "anomaly_injections.json": {"schema_version": 1, "injections": []},
        }

    @staticmethod
    def _job(
        job_id: str,
        compute: int,
        memory: int,
        *,
        cross: bool = False,
    ) -> dict:
        return {
            "job_id": job_id,
            "compute_shares": compute,
            "memory_shares": memory,
            "lease_duration_ms": 10000,
            "priority": "normal",
            "quota_group": "g",
            "preemptible": True,
            "allow_cross_card": cross,
            "arrival_round": 1,
        }

    def test_cross_card_stitches_multiple_card_local_leases(self) -> None:
        documents = self._base()
        documents["jobs.json"] = {
            "schema_version": 1,
            "jobs": [self._job("j1", 24, 12, cross=True)],
        }
        report = self._engine(documents).run_lease_round(1)
        self.assertEqual(len(report["grants"]), 2)
        self.assertEqual({item["card_id"] for item in report["grants"]}, {"a", "b"})
        self.assertGreaterEqual(
            sum(item["compute_length"] for item in report["grants"]), 24
        )
        self.assertGreaterEqual(
            sum(item["memory_length"] for item in report["grants"]), 12
        )

    def test_memory_constraint_is_first_when_compute_remains(self) -> None:
        documents = self._base()
        documents["accelerator_cards.json"]["cards"] = [
            documents["accelerator_cards.json"]["cards"][0]
        ]
        documents["jobs.json"] = {
            "schema_version": 1,
            "jobs": [
                self._job("j1", 4, 8),
                self._job("j2", 4, 4),
            ],
        }
        report = self._engine(documents).run_lease_round(1)
        self.assertEqual(report["denials"][0]["job_id"], "j2")
        self.assertEqual(
            report["denials"][0]["first_constraint"]["constraint"],
            "card_memory_remaining",
        )
        self.assertEqual(
            report["denials"][0]["first_constraint"]["dimension"], "memory"
        )

    def test_quota_constraint_is_first(self) -> None:
        documents = self._base()
        documents["lease_config.json"]["quota_group_compute_limits"]["g"] = 4
        documents["jobs.json"] = {
            "schema_version": 1,
            "jobs": [self._job("j1", 8, 4)],
        }
        report = self._engine(documents).run_lease_round(1)
        self.assertEqual(
            report["denials"][0]["first_constraint"]["constraint"],
            "quota_group_compute_limit",
        )


if __name__ == "__main__":
    unittest.main()
