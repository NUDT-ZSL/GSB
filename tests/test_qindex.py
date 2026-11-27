from __future__ import annotations

from pathlib import Path
import unittest

from qindex.boundaries import run_boundary_samples
from qindex.generator import generate_workload
from qindex.loader import load_workload
from qindex.model import make_index_key
from qindex.solver import solve_workload


ROOT = Path(__file__).resolve().parent.parent


def _event(
    event_id: str,
    time: int,
    batch_prefix: str,
    process: str = "冲压",
    shift: str = "早班",
    station: str = "ST-CY-01",
) -> dict:
    return {
        "event_id": event_id,
        "process": process,
        "shift": shift,
        "batch_prefix": batch_prefix,
        "logical_time": time,
        "station": station,
        "metric": "轮廓尺寸",
        "value": 1.0,
        "result": "合格",
    }


def _large_config(flush_threshold: int) -> dict:
    return {
        "flush_threshold": flush_threshold,
        "merge_invalid_ratio": 1.0,
        "partition_max_bytes": 100000,
        "levels": [
            {"name": "L0", "max_segments": 10, "max_bytes": 100000},
            {"name": "L1", "max_segments": 10, "max_bytes": 200000},
        ],
    }


class QualityIndexSolverTests(unittest.TestCase):
    def test_generated_workload_matches_reference_and_ledger(self) -> None:
        report = solve_workload(generate_workload())
        self.assertTrue(report["summary"]["all_ok"])
        self.assertTrue(
            report["summary"]["all_queries_match_reference"]
        )
        self.assertTrue(report["summary"]["all_states_match_reference"])
        self.assertTrue(
            report["summary"]["all_partition_accounting_ok"]
        )
        self.assertGreater(
            report["summary"]["action_counts"].get("compact", 0), 0
        )
        self.assertGreater(
            report["summary"]["action_counts"].get("promote", 0), 0
        )

    def test_composite_key_order(self) -> None:
        keys = [
            make_index_key("焊装", "早班", "LOT-B", 2, "E2"),
            make_index_key("冲压", "中班", "LOT-Z", 2, "E3"),
            make_index_key("冲压", "早班", "LOT-A", 9, "E4"),
            make_index_key("冲压", "早班", "LOT-A", 2, "E1"),
            make_index_key("冲压", "早班", "LOT-A", 2, "E0"),
        ]
        self.assertEqual(
            sorted(keys),
            [
                make_index_key("冲压", "中班", "LOT-Z", 2, "E3"),
                make_index_key("冲压", "早班", "LOT-A", 2, "E0"),
                make_index_key("冲压", "早班", "LOT-A", 2, "E1"),
                make_index_key("冲压", "早班", "LOT-A", 9, "E4"),
                make_index_key("焊装", "早班", "LOT-B", 2, "E2"),
            ],
        )

    def test_same_timestamp_burst_is_not_split_by_flush(self) -> None:
        workload = {
            "format": "quality-index-workload/v1",
            "seed": 77,
            "config": _large_config(3),
            "timeline": [
                {
                    "time": 1,
                    "writes": [
                        _event(f"BURST-{index:03d}", 1, "LOT-BURST-001")
                        for index in range(5)
                    ],
                    "invalidations": [],
                    "queries": [
                        {
                            "query_id": "Q-BURST",
                            "process": "冲压",
                            "shift": "早班",
                            "batch_prefix": "LOT-BURST-",
                        }
                    ],
                }
            ],
        }
        report = solve_workload(workload)
        flush_actions = [
            action
            for step in report["steps"]
            for action in step["actions"]
            if action["action"] == "flush"
        ]
        self.assertEqual(len(flush_actions), 1)
        self.assertEqual(flush_actions[0]["entry_count"], 5)
        self.assertGreater(flush_actions[0]["threshold_overshoot"], 0)
        query = report["steps"][0]["queries"][0]
        self.assertEqual(len(query["hits"]), 5)
        self.assertEqual(
            len({hit["source"]["segment_id"] for hit in query["hits"]}),
            1,
        )
        self.assertTrue(report["summary"]["all_ok"])

    def test_prefixed_query_is_time_ordered_across_segments(self) -> None:
        timeline = []
        event_seq = 1
        for time in range(1, 5):
            timeline.append(
                {
                    "time": time,
                    "writes": [
                        _event(
                            f"SEQ-{event_seq:03d}",
                            time,
                            "LOT-SEQ-001",
                        )
                    ],
                    "invalidations": [],
                    "queries": [],
                }
            )
            event_seq += 1
        timeline.append(
            {
                "time": 5,
                "writes": [],
                "invalidations": [],
                "queries": [
                    {
                        "query_id": "Q-SEQ",
                        "process": "冲压",
                        "shift": "早班",
                        "batch_prefix": "LOT-SEQ-",
                    }
                ],
            }
        )
        workload = {
            "format": "quality-index-workload/v1",
            "seed": 78,
            "config": _large_config(1),
            "timeline": timeline,
        }
        report = solve_workload(workload)
        query = report["steps"][-1]["queries"][0]
        self.assertEqual(
            [hit["logical_time"] for hit in query["hits"]],
            [1, 2, 3, 4],
        )
        self.assertGreater(len(query["hits"]), 1)
        self.assertTrue(report["summary"]["all_ok"])

    def test_manual_boundary_samples(self) -> None:
        report = run_boundary_samples(ROOT / "samples")
        self.assertTrue(report["all_passed"])
        self.assertEqual(report["sample_count"], 5)

    def test_deleted_segments_are_absent_from_final_state(self) -> None:
        report = solve_workload(
            load_workload(ROOT / "samples" / "04_capacity_only.json")
        )
        deleted = {
            segment_id
            for action in report["reclamation_actions"]
            for segment_id in action["source_segment_ids"]
        }
        self.assertTrue(deleted)
        live_ids = {
            segment["segment_id"]
            for segment in report["final_segment_manifest"]
        }
        view_ids = {
            segment["segment_id"]
            for view in report["final_partition_views"]
            for level in view["levels"]
            for segment in level["segments"]
        }
        self.assertTrue(deleted.isdisjoint(live_ids))
        self.assertTrue(deleted.isdisjoint(view_ids))

    def test_every_compaction_ledger_balances(self) -> None:
        report = solve_workload(
            load_workload(ROOT / "samples" / "05_ratio_and_capacity.json")
        )
        self.assertTrue(report["reclamation_actions"])
        for action in report["reclamation_actions"]:
            self.assertTrue(action["ledger_ok"])
            self.assertEqual(
                action["partition_bytes_before"],
                action["partition_bytes_after"]
                + action["reclaimed_bytes"],
            )
            self.assertEqual(
                action["deleted_segment_bytes"],
                action["rewritten_segment_bytes"]
                + action["reclaimed_bytes"],
            )


if __name__ == "__main__":
    unittest.main()
