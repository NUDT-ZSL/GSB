"""固定种子真值验收：聚类边界、影响面、账目一致、增量重算一致。"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from incident_timeline.normalize import EventValidationError  # noqa: E402
from incident_timeline.pipeline import run_full, run_incremental  # noqa: E402
from incident_timeline.registry import load_registry  # noqa: E402
from incident_timeline.storage import connect  # noqa: E402
from incident_timeline.timeline import build_timeline  # noqa: E402
from incident_timeline.models import FaultCluster  # noqa: E402


REGISTRY_PATH = ROOT / "config" / "dependency_registry.json"
SAMPLE_DIR = ROOT / "samples"


class IncidentTimelineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = load_registry(str(REGISTRY_PATH))
        with open(SAMPLE_DIR / "ground_truth.json", encoding="utf-8") as handle:
            cls.truth = json.load(handle)
        cls.params = cls.truth["window_params"]

    def _full(self, events_name, db_name, batch="batch-full"):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / db_name
            connection = connect(str(db_path))
            try:
                result = run_full(
                    str(SAMPLE_DIR / events_name), self.registry, dict(self.params), batch
                )
                from incident_timeline.storage import save_run

                save_run(connection, result)
            finally:
                connection.close()
        return result

    def test_batch1_ground_truth(self):
        result = self._full("events_batch1.jsonl", "b1.db")
        truth_b1 = self.truth["batch1"]
        faults = result["faults"]

        self.assertEqual(len(faults), len(truth_b1["faults"]))
        by_label = {fault.fault_id: fault for fault in faults}
        for index, expected in enumerate(truth_b1["faults"], start=1):
            fault = by_label[f"F-{index:03d}"]
            for service in expected["services_contains"]:
                self.assertIn(service, fault.services)
            self.assertEqual(fault.root_cause_ranking[0]["service"], expected["root_cause_top_service"])
            self.assertEqual(fault.affected_downstream, expected["affected_downstream"])
            self.assertEqual(fault.is_closed, expected["is_closed_after_batch1"])
            self.assertAlmostEqual(
                fault.start_ts, expected["start_near"],
                delta=expected["tolerance_ms"],
            )
            self.assertAlmostEqual(
                fault.end_ts, expected["end_near"],
                delta=expected["tolerance_ms"],
            )

        removed_ids = {record.removed_event_id for record in result["dedup_records"]}
        self.assertEqual(removed_ids, set(truth_b1["duplicate_removed_event_ids"]))
        self.assertEqual(len(result["dedup_records"]), truth_b1["duplicate_count"])

        corroborating = {
            event_id for fault in faults for event_id in fault.corroborating_ids
        }
        corroborating.update(
            event_id
            for context in result["context_clusters"]
            for event_id in context.get("corroborating_ids", [])
        )
        self.assertEqual(corroborating, set(truth_b1["corroborating_event_ids"]))

        context_services = {
            service
            for context in result["context_clusters"]
            for service in context["services"]
        }
        self.assertEqual(context_services, set(truth_b1["context_only_services"]))

        open_ids = [fault.fault_id for fault in faults if not fault.is_closed]
        self.assertEqual(open_ids, ["F-002"])
        open_fault = by_label["F-002"]
        self.assertIsNotNone(open_fault.end_ts)
        self.assertLessEqual(open_fault.end_ts, max(event.ts_ms for event in result["kept_events"]))
        self.assertTrue(any(item["basis"] == "open_loop" for item in open_fault.boundary_basis))

    def test_accounting_reconciles(self):
        result = self._full("events_batch1.jsonl", "acc.db")
        reconciliation = result["accounts"]["reconciliation"]
        self.assertTrue(reconciliation["all_ok"], msg=reconciliation["checks"])
        self.assertEqual(result["accounts"]["total_input"], self.truth["batch1"]["raw_event_count"])
        for row in result["accounts"]["by_source"] + result["accounts"]["by_service"]:
            self.assertEqual(
                row["raw_count"] - row["deduped_count"],
                row["removed_duplicate_count"],
            )
        total_by_source = sum(row["raw_count"] for row in result["accounts"]["by_source"])
        total_by_service = sum(row["raw_count"] for row in result["accounts"]["by_service"])
        self.assertEqual(total_by_source, result["accounts"]["total_input"])
        self.assertEqual(total_by_service, result["accounts"]["total_input"])

    def test_timeline_consistency_and_key_event_basis(self):
        result = self._full("events_batch1.jsonl", "tl.db")
        self.assertTrue(result["timeline"]["consistent"], msg=result["timeline"]["conflicts"])
        for entry in result["timeline"]["entries"]:
            self.assertTrue(entry["key_events"])
            self.assertTrue(entry["key_event_selection_basis"])
            key_ids = {item["event_id"] for item in entry["key_events"]}
            basis_ids = {item["event_id"] for item in entry["key_event_selection_basis"]}
            self.assertTrue(key_ids.issubset(basis_ids) or basis_ids.issuperset(key_ids))
        orders = [entry["start_ts"] for entry in result["timeline"]["entries"]]
        self.assertEqual(orders, sorted(orders))

    def test_timeline_detects_overlap_conflict(self):
        result = self._full("events_batch1.jsonl", "conflict.db")
        event_index = {event.event_id: event for event in result["kept_events"]}
        faults = list(result["faults"])
        overlapping = FaultCluster(
            fault_id="F-X",
            candidate_ids=["C-x"],
            event_ids=[],
            corroborating_ids=[],
            services=["auth-service"],
            start_ts=faults[0].start_ts + 1,
            end_ts=faults[0].end_ts,
            is_closed=True,
            open_triggers=[],
            root_cause_ranking=[],
            affected_downstream=[],
            not_on_dependency_chain=[],
            impact_summary="人为制造的重叠故障",
            boundary_basis=[],
        )
        conflict_report = build_timeline(faults + [overlapping], event_index)
        self.assertFalse(conflict_report["consistent"])
        self.assertTrue(
            any(conflict["type"] == "range_overlap" for conflict in conflict_report["conflicts"])
        )

    def test_merge_records_traceable(self):
        result = self._full("events_batch1.jsonl", "merge.db")
        self.assertTrue(result["merge_records"])
        for record in result["merge_records"]:
            self.assertTrue(record.left_candidate.startswith("C-"))
            self.assertTrue(record.right_candidate.startswith("C-"))
            self.assertIn("->", record.basis_edge)
            self.assertLessEqual(record.min_gap_ms, record.window_ms)
        merged_candidate_ids = {
            candidate_id for fault in result["faults"] for candidate_id in fault.candidate_ids
        }
        context_candidate_ids = {
            candidate_id
            for context in result["context_clusters"]
            for candidate_id in context["candidate_ids"]
        }
        all_candidate_ids = {candidate.candidate_id for candidate in result["candidates"]}
        self.assertEqual(merged_candidate_ids | context_candidate_ids, all_candidate_ids)

    def test_unregistered_service_raises_locatable_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "invalid.db"
            connection = connect(str(db_path))
            try:
                with self.assertRaises(EventValidationError) as ctx:
                    run_full(
                        str(SAMPLE_DIR / "events_invalid_service.jsonl"),
                        self.registry,
                        dict(self.params),
                        "bad",
                    )
            finally:
                connection.close()
        message = str(ctx.exception)
        self.assertIn("events_invalid_service.jsonl:1", message)
        self.assertIn("not-registered-svc", message)

    def test_incremental_matches_full_and_flags_boundary_change(self):
        truth_inc = self.truth["batch2_incremental"]
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "inc.db"
            connection = connect(str(db_path))
            try:
                from incident_timeline.storage import save_run

                full_b1 = run_full(
                    str(SAMPLE_DIR / "events_batch1.jsonl"),
                    self.registry,
                    dict(self.params),
                    "batch1",
                )
                save_run(connection, full_b1)
                incremental = run_incremental(
                    str(SAMPLE_DIR / "events_batch1_plus_batch2.jsonl"),
                    self.registry,
                    dict(self.params),
                    "batch2",
                    connection,
                )
                save_run(connection, incremental)
            finally:
                connection.close()

            full_reference = self._full("events_batch1_plus_batch2.jsonl", "ref.db", "all")

        self.assertEqual(
            incremental["accounts"]["total_input"], truth_inc["raw_event_count_total"]
        )
        self.assertEqual(
            len(incremental["dedup_records"]), truth_inc["duplicate_count_total"]
        )
        self.assertEqual(
            {record.removed_event_id for record in incremental["dedup_records"]},
            set(self.truth["batch1"]["duplicate_removed_event_ids"])
            | set(truth_inc["new_duplicate_event_ids"]),
        )

        self._assert_faults_fieldwise_equal(full_reference, incremental)
        self._assert_timeline_fieldwise_equal(full_reference, incremental)
        self._assert_accounts_fieldwise_equal(full_reference, incremental)

        changes = incremental["incremental_changes"]
        change_map = {
            change["fault_id"]: change["change_type"]
            for change in changes
            if change["fault_id"]
        }
        expected_changes = {
            "F-001": "verified_unchanged",
            "F-002": "boundary_changed",
            "F-003": "verified_unchanged",
            "F-004": "new",
        }
        for fault_id, change_type in expected_changes.items():
            self.assertEqual(change_map[fault_id], change_type)

        f2 = next(fault for fault in incremental["faults"] if fault.fault_id == "F-002")
        self.assertTrue(f2.is_closed)
        self.assertAlmostEqual(
            f2.end_ts, truth_inc["faults"][1]["end_near"],
            delta=truth_inc["faults"][1]["tolerance_ms"],
        )
        f4 = next(fault for fault in incremental["faults"] if fault.fault_id == "F-004")
        self.assertEqual(f4.root_cause_ranking[0]["service"], "mq-service")
        self.assertEqual(f4.affected_downstream, ["msg-delivery"])

    def _assert_faults_fieldwise_equal(self, full_result, inc_result):
        full_faults = {fault.fault_id: fault for fault in full_result["faults"]}
        for inc_fault in inc_result["faults"]:
            full_fault = full_faults[inc_fault.fault_id]
            for attr in (
                "candidate_ids", "event_ids", "corroborating_ids", "services",
                "start_ts", "end_ts", "is_closed", "open_triggers",
                "root_cause_ranking", "affected_downstream", "not_on_dependency_chain",
                "boundary_basis",
            ):
                self.assertEqual(
                    getattr(full_fault, attr),
                    getattr(inc_fault, attr),
                    msg=f"故障 {inc_fault.fault_id} 字段 {attr} 增量与全量不一致",
                )

    def _assert_timeline_fieldwise_equal(self, full_result, inc_result):
        full_entries = {entry["fault_id"]: entry for entry in full_result["timeline"]["entries"]}
        for inc_entry in inc_result["timeline"]["entries"]:
            full_entry = dict(full_entries[inc_entry["fault_id"]])
            current_entry = dict(inc_entry)
            full_entry.pop("changed_vs_previous", None)
            current_entry.pop("changed_vs_previous", None)
            self.assertEqual(
                full_entry, current_entry,
                msg=f"时间线 {inc_entry['fault_id']} 增量与全量逐字段不一致",
            )

    def _assert_accounts_fieldwise_equal(self, full_result, inc_result):
        for dimension in ("by_source", "by_service"):
            full_rows = {row["key"]: row for row in full_result["accounts"][dimension]}
            inc_rows = {row["key"]: row for row in inc_result["accounts"][dimension]}
            self.assertEqual(set(full_rows), set(inc_rows))
            for key in full_rows:
                self.assertEqual(full_rows[key], inc_rows[key])
        self.assertEqual(
            full_result["accounts"]["reconciliation"]["all_ok"],
            inc_result["accounts"]["reconciliation"]["all_ok"],
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
