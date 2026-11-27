from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from rebalance_kernel.cli import execute
from rebalance_kernel.generator import generate_bundle
from rebalance_kernel.model import KeyedEvent, MemberEvent
from rebalance_kernel.normalize import normalize_project, normalization_report


class KernelIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tempdir = tempfile.TemporaryDirectory()
        cls.output_dir = Path(cls.tempdir.name)
        cls.result = execute(
            seed=20261128,
            output_dir=cls.output_dir,
            imbalance_threshold=0.25,
            inflight_limit=2,
        )
        cls.generated_dir = cls.output_dir / "generated"
        cls.generated = {
            "project_v1": json.loads(
                (cls.generated_dir / "project_v1.json").read_text("utf-8")
            ),
            "project_v2": json.loads(
                (cls.generated_dir / "project_v2.json").read_text("utf-8")
            ),
            "project_v3": json.loads(
                (cls.generated_dir / "project_v3.json").read_text("utf-8")
            ),
            "keyed_events": json.loads(
                (cls.generated_dir / "keyed_events.json").read_text("utf-8")
            ),
        }

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tempdir.cleanup()

    def test_three_schema_versions_normalize_identically(self) -> None:
        projects = {
            version: normalize_project(self.generated[f"project_{version}"])
            for version in ("v1", "v2", "v3")
        }
        report = normalization_report(projects)
        self.assertTrue(report["canonical_equal"])
        self.assertEqual(len(set(report["canonical_sha256"].values())), 1)
        self.assertGreater(report["raw_difference_count"], 0)
        self.assertEqual(
            projects["v2"].topics["deal_confirmation"].partitions,
            (0, 1, 2, 3),
        )
        self.assertEqual(
            projects["v2"].members["consumer-b"].session_timeout_ms,
            7500,
        )

    def test_every_normalized_field_has_provenance(self) -> None:
        report = self.result["normalization_report"]
        mappings = {
            item["normalized_field"]: item for item in report["field_mappings"]
        }
        for member_id in self.result["runtime"]["project"]["members"]:
            for field in (
                f"members.{member_id}.member_id",
                f"members.{member_id}.processing_rate",
                f"members.{member_id}.session_timeout_ms",
                f"members.{member_id}.subscriptions",
            ):
                self.assertIn(field, mappings)
                self.assertEqual(set(mappings[field]["by_version"]), {"v1", "v2", "v3"})
        for topic in self.result["runtime"]["project"]["topics"]:
            name_field = f"topics.{topic}.name"
            self.assertIn(name_field, mappings)
            self.assertEqual(
                set(mappings[name_field]["by_version"]), {"v1", "v2", "v3"}
            )
            field = f"topics.{topic}.partitions"
            self.assertIn(field, mappings)
            self.assertEqual(set(mappings[field]["by_version"]), {"v1", "v2", "v3"})

    def test_version_runtime_results_are_identical(self) -> None:
        self.assertTrue(self.result["version_equivalence"]["equal"])
        self.assertEqual(
            len(set(self.result["version_equivalence"]["runtime_sha256"].values())),
            1,
        )

    def test_each_partition_key_has_one_owner_each_round(self) -> None:
        key_to_partition: dict[str, str] = {}
        for raw in self.generated["keyed_events"]:
            event = KeyedEvent.from_dict(raw)
            key_to_partition.setdefault(event.partition_key, event.pid)
            self.assertEqual(key_to_partition[event.partition_key], event.pid)
        for round_report in self.result["runtime"]["rounds"]:
            self.assertTrue(round_report["single_owner_check"]["passed"])
            by_partition = round_report["assignment"]["by_partition"]
            for pid, record in by_partition.items():
                self.assertEqual(pid, f"{record['topic']}:{record['partition']}")
                self.assertIsInstance(record["member_id"], (str, type(None)))
                if record["member_id"] is not None:
                    self.assertIn(
                        record["member_id"], round_report["participants"]
                    )

    def test_redelivery_is_complete_continuous_and_unique(self) -> None:
        events_by_pid: dict[str, list[KeyedEvent]] = {}
        for raw in self.generated["keyed_events"]:
            event = KeyedEvent.from_dict(raw)
            events_by_pid.setdefault(event.pid, []).append(event)
        all_redelivered: list[str] = []
        for round_report in self.result["runtime"]["rounds"]:
            verification = round_report["redelivery"]["verification"]
            self.assertTrue(verification["passed"])
            for record in round_report["redelivery"]["records"]:
                self.assertEqual(
                    record["count"],
                    record["end_offset"] - record["start_offset"] + 1,
                )
                self.assertTrue(record["offsets_continuous"])
                self.assertTrue(record["within_partition_bounds"])
                event_ids = record["event_ids"]
                self.assertEqual(len(event_ids), len(set(event_ids)))
                events = events_by_pid[record["partition"]]
                self.assertLess(record["end_offset"], len(events))
                offsets = [
                    events[offset].offset
                    for offset in range(
                        record["start_offset"], record["end_offset"] + 1
                    )
                ]
                self.assertEqual(
                    offsets,
                    list(
                        range(
                            record["start_offset"], record["end_offset"] + 1
                        )
                    ),
                )
                all_redelivered.extend(event_ids)
        self.assertEqual(len(all_redelivered), len(set(all_redelivered)))

    def test_local_rate_change_matches_full_rebalance(self) -> None:
        local_rounds = [
            item
            for item in self.result["runtime"]["rounds"]
            if item["local_rebalance"] is not None
        ]
        self.assertGreaterEqual(len(local_rounds), 2)
        for item in local_rounds:
            local = item["local_rebalance"]
            self.assertTrue(local["consistent_with_full_rebalance"])
            self.assertEqual(local["differences"], [])
        round_2000 = next(
            item
            for item in local_rounds
            if item["at_ms"] == 2000
        )
        self.assertEqual(round_2000["local_rebalance"]["affected_topics"], sorted(
            round_2000["local_rebalance"]["affected_topics"]
        ))

    def test_expiration_rounds_are_deterministic(self) -> None:
        expirations = self.result["runtime"]["verification"]["expiration_rounds"]
        self.assertEqual(
            [(item["at_ms"], item["members"]) for item in expirations],
            [(12001, ["consumer-c"]), (23000, ["consumer-a"])],
        )
        for round_report in self.result["runtime"]["rounds"]:
            expired = set(round_report["trigger"]["expired_members"])
            if not expired:
                continue
            self.assertTrue(round_report["trigger"]["rebalanced"])
            for record in round_report["assignment"]["by_partition"].values():
                self.assertNotIn(record["member_id"], expired)
            self.assertGreater(round_report["redelivery"]["reclaimed_event_count"], 0)

    def test_edge_cases(self) -> None:
        edge = {
            item["name"]: item for item in self.result["edge_cases"]
        }
        self.assertEqual(
            set(edge),
            {
                "more_members_than_partitions",
                "simultaneous_join_leave",
                "timeout_boundary",
                "high_key_concentration",
            },
        )
        for item in edge.values():
            self.assertTrue(item["verification"]["passed"])

        more_members = edge["more_members_than_partitions"]
        assigned = [
            record
            for record in more_members["final_assignment"].values()
            if record["member_id"] is not None
        ]
        self.assertEqual(len(assigned), 1)
        self.assertTrue(more_members["over_threshold_members"])

        simultaneous = edge["simultaneous_join_leave"]
        final_round = simultaneous["rounds"][-1]
        self.assertEqual(
            final_round["participants"], ["edge-a", "edge-d", "edge-e"]
        )
        self.assertIn("member_leave", final_round["trigger_types"])
        self.assertIn("member_join", final_round["trigger_types"])

        boundary = edge["timeout_boundary"]
        round_5000 = next(
            item for item in boundary["rounds"] if item["at_ms"] == 5000
        )
        self.assertEqual(
            round_5000["liveness_before"]["edge-a"]["verdict"], "alive"
        )
        self.assertEqual(
            round_5000["liveness_before"]["edge-a"]["boundary"],
            "exact_session_timeout",
        )
        self.assertEqual(
            round_5000["liveness_after"]["edge-a"]["verdict"], "alive"
        )
        round_10001 = next(
            item for item in boundary["rounds"] if item["at_ms"] == 10001
        )
        self.assertEqual(
            round_10001["liveness_after"]["edge-a"]["verdict"], "expired"
        )

        concentrated = edge["high_key_concentration"]
        self.assertEqual(
            concentrated["key_distribution"]["concentrated_topic:0"], 60
        )
        self.assertEqual(
            sum(
                1
                for record in concentrated["final_assignment"].values()
                if record["member_id"] is not None
            ),
            4,
        )

    def test_fixed_seed_generation_is_byte_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as first:
            with tempfile.TemporaryDirectory() as second:
                paths_a = generate_bundle(77, Path(first))
                paths_b = generate_bundle(77, Path(second))
                for key in (
                    "project_v1",
                    "project_v2",
                    "project_v3",
                    "member_events",
                    "keyed_events",
                    "edge_cases",
                    "manifest",
                ):
                    self.assertEqual(
                        paths_a[key].read_bytes(),
                        paths_b[key].read_bytes(),
                        key,
                    )

    def test_summary_passes(self) -> None:
        self.assertTrue(self.result["summary"]["passed"])
        self.assertTrue(self.result["runtime"]["verification"]["passed"])
        self.assertTrue(
            (
                self.output_dir / "normalization_report.json"
            ).exists()
        )
        self.assertTrue((self.output_dir / "rebalance_report.json").exists())


class AssignmentUnitTest(unittest.TestCase):
    def test_too_few_partitions_are_still_single_owner(self) -> None:
        project = normalize_project(
            {
                "schema": "v1",
                "consumer_group": "g",
                "members": [
                    {
                        "member_id": "m1",
                        "processing_rate": 1,
                        "session_timeout_ms": 1000,
                        "subscriptions": ["t"],
                    },
                    {
                        "member_id": "m2",
                        "processing_rate": 2,
                        "session_timeout_ms": 1000,
                        "subscriptions": ["t"],
                    },
                ],
                "partitions": [
                    {"topic": "t", "partition": 0},
                ],
            }
        )
        events = [
            KeyedEvent(
                event_id="e",
                topic="t",
                partition=0,
                offset=0,
                partition_key="k",
                available_at_ms=0,
                event_type="x",
            )
        ]
        member_events = [
            MemberEvent("j1", 0, "join", "m1", source_index=0),
            MemberEvent("j2", 0, "join", "m2", source_index=1),
        ]
        from rebalance_kernel.engine import RebalanceEngine

        result = RebalanceEngine(project, member_events, events).run()
        assignment = result["rounds"][0]["assignment"]["by_partition"]
        self.assertEqual(len(assignment), 1)
        self.assertIn(assignment["t:0"]["member_id"], {"m1", "m2"})
        self.assertTrue(result["verification"]["passed"])


if __name__ == "__main__":
    unittest.main()
