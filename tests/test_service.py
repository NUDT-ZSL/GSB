from __future__ import annotations

import copy
import json
import tempfile
import time
import unittest
from pathlib import Path

from dialogue_risk.errors import AppError
from dialogue_risk.service import DomainService
from tests.helpers import DATA


class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.runtime = Path(self.temp_dir.name) / "runtime"
        self.service = DomainService(DATA, self.runtime)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def assert_error(self, code: str, callable_):
        with self.assertRaises(AppError) as context:
            callable_()
        self.assertEqual(context.exception.code, code)
        return context.exception

    def test_query_and_batch_are_consistent(self) -> None:
        batch = self.service.run_batch()
        batch_record = next(
            item for item in batch["results"] if item["conv_id"] == "conv-0001"
        )
        query = self.service.query("conv-0001", budget_ms=1000.0)
        for field_name in (
            "sentiment_score",
            "sentiment_label",
            "risk_score",
            "risk_level",
            "risk_candidates",
            "message_ids",
        ):
            self.assertEqual(query[field_name], batch_record[field_name])
        self.assertTrue(query["query_meta"]["consistency"]["ok"])
        self.assertGreaterEqual(
            query["query_meta"]["latency"]["message_count"], 1
        )
        self.assertGreater(
            query["query_meta"]["latency"]["risk_word_count"], 0
        )

    def test_query_detects_inconsistent_batch_record(self) -> None:
        expected = self.service._snapshot.results["conv-0001"]
        expected["risk_level"] = "critical"
        error = self.assert_error(
            "E_INCONSISTENT",
            lambda: self.service.query("conv-0001", budget_ms=1000.0),
        )
        self.assertEqual(error.details["category"], "risk_level")
        self.assertEqual(error.details["conv_id"], "conv-0001")

    def test_inconsistency_reports_first_differing_message(self) -> None:
        expected = self.service._snapshot.results["conv-0001"]
        expected["messages"][1]["sentiment_score"] += 0.1
        error = self.assert_error(
            "E_INCONSISTENT",
            lambda: self.service.query("conv-0001", budget_ms=1000.0),
        )
        self.assertEqual(error.details["category"], "message.sentiment_score")
        self.assertEqual(error.details["first_diff_msg_no"], 2)

    def test_latency_budget_reports_real_measurement_and_stages(self) -> None:
        error = self.assert_error(
            "E_LATENCY_BUDGET",
            lambda: self.service.query(
                "conv-0001", budget_ms=20.0, inject_delay_ms=30.0
            ),
        )
        self.assertGreaterEqual(error.details["observed_ms"], 25.0)
        self.assertEqual(error.details["budget_ms"], 20.0)
        self.assertTrue(error.details["completed_stages"])
        self.assertEqual(
            error.details["measurement"],
            "time.perf_counter_ns local processing loop",
        )

    def test_text_edit_invalidates_only_affected_cache_entries(self) -> None:
        entries = self.service.cache.entries()
        edited = next(entry for entry in entries if entry.conv_id == "conv-0001")
        unaffected = next(entry for entry in entries if entry.conv_id != "conv-0001")
        commit = self.service.edit_text(
            "conv-0001", 2, "对方让我提供验证码并转账，我非常失望"
        )
        keys_after = set(self.service.cache.keys())
        self.assertNotIn(edited.key, keys_after)
        self.assertIn(unaffected.key, keys_after)
        self.assertEqual(commit["invalidated_cache_entries"], 1)
        self.assertGreaterEqual(commit["preserved_cache_entries"], 7)
        query = self.service.query("conv-0001", budget_ms=1000.0)
        terms = [
            hit["term"]
            for message in query["messages"]
            for hit in message["risk_hits"]
        ]
        self.assertIn("验证码", terms)
        self.assertEqual(query["messages"][1]["text"], "对方让我提供验证码并转账，我非常失望")
        self.service.self_check()

    def test_weight_edit_changes_version_and_aggregation(self) -> None:
        before = self.service.query("conv-0002", budget_ms=1000.0)
        commit = self.service.edit_weight("conv-0002", 3, 2.5)
        after = self.service.query("conv-0002", budget_ms=1000.0)
        self.assertEqual(after["messages"][2]["weight"], 2.5)
        self.assertEqual(
            commit["versions"]["weights"], before["query_meta"]["versions"]["weights"] + 1
        )
        self.assertEqual(
            commit["versions"]["results"],
            before["query_meta"]["versions"]["results"] + 1,
        )
        self.service.self_check()

    def test_split_and_merge_keep_msg_no_continuous(self) -> None:
        original_count = len(
            self.service._snapshot.conversations["conv-0003"]["messages"]
        )
        split_commit = self.service.split_conversation(
            "conv-0003", 4, "conv-0003-slice"
        )
        batch = self.service.run_batch()
        records = {
            record["conv_id"]: record
            for record in batch["results"]
            if record["conv_id"] in {"conv-0003", "conv-0003-slice"}
        }
        self.assertEqual(sorted(records), ["conv-0003", "conv-0003-slice"])
        self.assertEqual(
            [message["msg_no"] for message in records["conv-0003"]["messages"]],
            list(range(1, len(records["conv-0003"]["messages"]) + 1)),
        )
        self.assertEqual(
            [message["msg_no"] for message in records["conv-0003-slice"]["messages"]],
            list(range(1, len(records["conv-0003-slice"]["messages"]) + 1)),
        )
        self.assertEqual(
            len(records["conv-0003"]["messages"])
            + len(records["conv-0003-slice"]["messages"]),
            original_count,
        )
        merge_commit = self.service.merge_conversations(
            "conv-0003", "conv-0003-slice"
        )
        merged = next(
            record
            for record in self.service.run_batch()["results"]
            if record["conv_id"] == "conv-0003"
        )
        self.assertEqual(
            [message["msg_no"] for message in merged["messages"]],
            list(range(1, original_count + 1)),
        )
        self.assertIn("conv-0003-slice", {
            item
            for item in split_commit["affected_conversations"]
        })
        self.assertIn("conv-0003", {
            item for item in merge_commit["affected_conversations"]
        })
        self.service.self_check()

    def test_stage_busy_is_immediate(self) -> None:
        with self.service.commit_gate.hold_for_test():
            started = time.perf_counter()
            error = self.assert_error(
                "E_STAGE_BUSY",
                lambda: self.service.edit_weight("conv-0001", 1, 1.1),
            )
            elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 0.1)
        self.assertFalse(error.details["blocking"])

    def test_resource_limit_reports_degraded_category(self) -> None:
        config = copy.deepcopy(self.service._snapshot.config)
        config["resource_limits"]["max_conversations"] = 1
        error = self.assert_error(
            "E_RESOURCE_LIMIT", lambda: self.service.update_config(config)
        )
        self.assertEqual(error.details["degraded_category"], "conversations")

        message_config = copy.deepcopy(self.service._snapshot.config)
        message_config["resource_limits"]["max_messages"] = 1
        message_error = self.assert_error(
            "E_RESOURCE_LIMIT",
            lambda: self.service.update_config(message_config),
        )
        self.assertEqual(message_error.details["degraded_category"], "messages")

        cache_config = copy.deepcopy(self.service._snapshot.config)
        cache_config["cache"]["max_entries"] = 1
        cache_config["resource_limits"]["max_cache_entries"] = 1
        cache_error = self.assert_error(
            "E_RESOURCE_LIMIT",
            lambda: self.service.update_config(cache_config),
        )
        self.assertEqual(cache_error.details["degraded_category"], "cache_entries")

    def test_self_check_detects_cache_tampering(self) -> None:
        key = self.service.cache.keys()[0]
        entry = self.service.cache._entries[key]
        entry.result["risk_level"] = "critical"
        error = self.assert_error("E_INCONSISTENT", self.service.self_check)
        self.assertEqual(error.details["category"], "cache_result")

    def test_restart_loads_latest_snapshot(self) -> None:
        self.service.edit_text("conv-0001", 1, "重启后仍应看到这句话")
        latest_version = self.service._snapshot.snapshot_version
        restarted = DomainService(DATA, self.runtime)
        self.assertEqual(restarted._snapshot.snapshot_version, latest_version)
        self.assertEqual(
            restarted._snapshot.results["conv-0001"]["messages"][0]["text"],
            "重启后仍应看到这句话",
        )
        restarted.self_check()

    def test_config_update_replaces_whole_snapshot(self) -> None:
        config = copy.deepcopy(self.service._snapshot.config)
        config["query"]["latency_budget_ms"] = 75.0
        commit = self.service.update_config(config)
        self.assertEqual(
            commit["versions"]["config"],
            self.service._snapshot.versions["config"],
        )
        self.assertGreater(commit["invalidated_cache_entries"], 1)
        self.assertEqual(self.service._snapshot.config["query"]["latency_budget_ms"], 75.0)
        self.service.self_check()

    def test_cache_policy_is_part_of_whole_config_replacement(self) -> None:
        disabled = copy.deepcopy(self.service._snapshot.config)
        disabled["cache"]["enabled"] = False
        self.service.update_config(disabled)
        self.assertFalse(self.service.cache.enabled)
        self.assertEqual(len(self.service.cache), 0)

        enabled = copy.deepcopy(self.service._snapshot.config)
        enabled["cache"]["enabled"] = True
        self.service.update_config(enabled)
        self.assertTrue(self.service.cache.enabled)
        self.assertEqual(len(self.service.cache), len(self.service._snapshot.conversations))
        self.service.self_check()


if __name__ == "__main__":
    unittest.main()

