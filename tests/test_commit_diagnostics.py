from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from mm_router.commit_control import CommitController
from mm_router.diagnostics import (
    build_diagnostic_report,
    verify_invalidation_plan,
    verify_recovery_consistency,
)
from mm_router.cache import InvalidationPlan
from mm_router.errors import (
    E_FORMAT_CORRUPT,
    E_INCONSISTENT,
    E_SNAPSHOT_ROLLBACK,
    E_STAGE_BUSY,
    WorkbenchError,
)
from mm_router.generate import generate_sample, read_requests
from mm_router.config_io import read_config
from mm_router.workbench import run_acceptance_demo


class CommitDiagnosticsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_config_and_checkpoint_share_a_nonblocking_stage_lock(self) -> None:
        controller = CommitController(self.root / "controller")
        controller.commit_config_snapshot(
            snapshot={"generation": 1},
            version=1,
        )
        with controller.lock.hold():
            with self.assertRaises(WorkbenchError) as config_error:
                controller.commit_config_snapshot(
                    snapshot={"generation": 2},
                    version=2,
                )
            self.assertEqual(config_error.exception.code, E_STAGE_BUSY)
            with self.assertRaises(WorkbenchError) as checkpoint_error:
                controller.commit_checkpoint_batch(
                    schedule_key="schedule-a",
                    completed_request_ids=["req-0000"],
                    decision_records={
                        "req-0000": {"request_id": "req-0000", "business_code": "OK"}
                    },
                    batch_index=0,
                )
            self.assertEqual(checkpoint_error.exception.code, E_STAGE_BUSY)

    def test_drift_forces_full_snapshot_replace_and_rollback_is_rejected(self) -> None:
        controller = CommitController(self.root / "controller")
        first = controller.commit_config_snapshot(
            snapshot={"generation": 1},
            version=1,
        )
        self.assertTrue(first.full_snapshot_replace)
        drift = controller.commit_config_snapshot(
            snapshot={"generation": 2},
            version=2,
            expected_parent_version=99,
            expected_parent_checksum="0" * 64,
        )
        self.assertTrue(drift.drift_detected)
        self.assertTrue(drift.full_snapshot_replace)
        self.assertEqual(controller.ledger.read().snapshot, {"generation": 2})
        with self.assertRaises(WorkbenchError) as caught:
            controller.commit_config_snapshot(
                snapshot={"generation": 1},
                version=1,
            )
        self.assertEqual(caught.exception.code, E_SNAPSHOT_ROLLBACK)

    def test_ledger_checksum_tamper_is_detected(self) -> None:
        controller = CommitController(self.root / "controller")
        controller.commit_config_snapshot(snapshot={"generation": 1}, version=1)
        envelope = json.loads(controller.ledger.path.read_text(encoding="utf-8"))
        envelope["snapshot"]["generation"] = 2
        controller.ledger.path.write_text(
            json.dumps(envelope, sort_keys=True),
            encoding="utf-8",
        )
        with self.assertRaises(WorkbenchError) as caught:
            controller.ledger.read()
        self.assertEqual(caught.exception.code, E_FORMAT_CORRUPT)

    def test_recovery_business_code_mismatch_reports_first_request_and_node(self) -> None:
        left = [
            {
                "request_id": "req-0001",
                "business_code": "OK",
                "nodes": [{"node": "N4_FUSION", "artifact_hash": "a"}],
            }
        ]
        right = [
            {
                "request_id": "req-0001",
                "business_code": "E_NO_ROUTE",
                "nodes": [{"node": "N4_FUSION", "artifact_hash": "a"}],
            }
        ]
        with self.assertRaises(WorkbenchError) as caught:
            verify_recovery_consistency(left, right)
        self.assertEqual(caught.exception.code, E_INCONSISTENT)
        self.assertEqual(caught.exception.first_request_id, "req-0001")
        self.assertEqual(caught.exception.first_node, "N4_FUSION")

    def test_diagnostic_report_reports_inconsistency_instead_of_raising(self) -> None:
        sample = generate_sample(self.root / "sample", request_count=4)
        requests = read_requests(sample["requests"])
        config = read_config(sample["config_v1"]).config
        from mm_router.cache import ContentAddressedCache
        from mm_router.routing import RoutingPipeline

        cache = ContentAddressedCache(self.root / "cache")
        pipeline = RoutingPipeline(cache)
        records = [pipeline.execute(request, config).to_dict() for request in requests]
        mutated = json.loads(json.dumps(records))
        mutated[1]["business_code"] = "E_INCONSISTENT"
        report = build_diagnostic_report(
            cache=cache,
            requests=requests,
            config=config,
            recovered_records=mutated,
            replay_records=records,
        )
        self.assertEqual(report.code, E_INCONSISTENT)
        self.assertIsNotNone(report.first_request_id)
        self.assertIsNotNone(report.first_node)

    def test_invalid_invalidation_plan_returns_structured_inconsistency(self) -> None:
        plan = InvalidationPlan(
            affected_request_ids=("req-0000",),
            unaffected_request_ids=(),
            added_request_ids=(),
            removed_request_ids=(),
            reused_nodes=("req-0000:N1_FEATURE",),
            recomputed_nodes=("req-0000:N1_FEATURE", "req-0000:N2_SIMILARITY"),
            covered_nodes=("req-0000:N1_FEATURE", "req-0000:N2_SIMILARITY"),
            earliest_node_by_request={"req-0000": "N2_SIMILARITY"},
        )
        with self.assertRaises(WorkbenchError) as caught:
            verify_invalidation_plan(plan)
        self.assertEqual(caught.exception.code, E_INCONSISTENT)
        self.assertEqual(caught.exception.first_request_id, "req-0000")
        self.assertEqual(caught.exception.first_node, "N4_FUSION")

    def test_end_to_end_acceptance_demo(self) -> None:
        summary = run_acceptance_demo(
            self.root / "demo",
            seed=20261127,
            use_processes=False,
        )
        self.assertEqual(summary["diagnostics"]["code"], "OK")
        self.assertTrue(summary["routing"]["byte_identical_replay"]["passed"])
        self.assertTrue(summary["invalidation"]["check"]["passed"])
        self.assertTrue(summary["recovery"]["check"]["passed"])
        self.assertEqual(
            {item["code"] for item in summary["commits"]["busy_errors"]},
            {E_STAGE_BUSY},
        )
        self.assertEqual(
            summary["commits"]["rollback_error"]["code"],
            E_SNAPSHOT_ROLLBACK,
        )
        self.assertEqual(
            summary["resource_adjudication"]["business_code"],
            "E_RESOURCE_ADJUSTED",
        )
        report_path = Path(summary["diagnostic_report"])
        self.assertTrue(report_path.is_file())
        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(report["code"], "OK")


if __name__ == "__main__":
    unittest.main()

