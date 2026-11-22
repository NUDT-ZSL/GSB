import tempfile
import unittest
from pathlib import Path

from intentkit.cache import (
    ContentStore,
    fingerprint_results,
    plan_incremental,
    run_incremental,
    tickets_hitting_rules,
)
from intentkit.errors import BizError
from intentkit.generator import (
    build_config_v1,
    build_config_v2,
    build_config_v3,
    correct_gold_label,
    generate_tickets,
)
from intentkit.matcher import match_exact
from intentkit.models import Ticket
from intentkit.snapshot import SnapshotStore


class CacheSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = ContentStore(self.root / "cache")
        self.snapshots = SnapshotStore(self.root / "snapshots")
        self.config_v1 = build_config_v1()
        self.config_v2 = build_config_v2()
        self.tickets = generate_tickets(count=64)

    def tearDown(self):
        self.temp.cleanup()

    def test_incremental_config_change_matches_full_rerun(self):
        plan_v1 = plan_incremental(
            self.tickets, self.tickets, self.config_v1, self.config_v1, "exact", self.store
        )
        results_v1, recomputed_v1 = run_incremental(
            self.tickets, self.config_v1, "exact", plan_v1, self.store
        )
        self.assertEqual(len(recomputed_v1), len(self.tickets))

        threshold_invalid = tickets_hitting_rules(
            self.tickets, self.config_v2, ["logistics_query"]
        )
        plan_v2 = plan_incremental(
            self.tickets,
            self.tickets,
            self.config_v1,
            self.config_v2,
            "exact",
            self.store,
            extra_invalid_ticket_ids=threshold_invalid,
        )
        inc_v2, recomputed_v2 = run_incremental(
            self.tickets, self.config_v2, "exact", plan_v2, self.store
        )
        full_v2 = [match_exact(ticket, self.config_v2) for ticket in self.tickets]
        self.assertEqual(
            fingerprint_results(full_v2), fingerprint_results(inc_v2)
        )
        self.assertIn("logistics_query", plan_v2.affected_rule_labels)
        self.assertTrue(plan_v2.reused_ticket_ids)

    def test_gold_correction_only_recomputes_one_ticket(self):
        plan_v1 = plan_incremental(
            self.tickets, self.tickets, self.config_v1, self.config_v1, "exact", self.store
        )
        run_incremental(self.tickets, self.config_v1, "exact", plan_v1, self.store)

        target_id = self.tickets[5].ticket_id
        corrected = correct_gold_label(self.tickets, target_id, "complaint")
        plan_gold = plan_incremental(
            self.tickets, corrected, self.config_v1, self.config_v1, "exact", self.store
        )
        self.assertEqual(plan_gold.recompute_ticket_ids, [target_id])
        results, recomputed = run_incremental(
            corrected, self.config_v1, "exact", plan_gold, self.store
        )
        full = [match_exact(ticket, self.config_v1) for ticket in corrected]
        self.assertEqual(fingerprint_results(full), fingerprint_results(results))
        self.assertEqual(len(recomputed), 1)

    def test_snapshot_rollback_rejected(self):
        self.snapshots.save(self.config_v2)
        with self.assertRaises(BizError) as context:
            self.snapshots.save(self.config_v1)
        self.assertEqual(context.exception.code, "E_SNAPSHOT_ROLLBACK")

    def test_unknown_version_rejected(self):
        self.snapshots.save(self.config_v1)
        with self.assertRaises(BizError) as context:
            self.snapshots.load(version=42)
        self.assertEqual(context.exception.code, "E_UNKNOWN_CONFIG_VERSION")

    def test_label_retirement_uses_fallback_or_raises(self):
        self.snapshots.save(build_config_v3())
        config_v3 = build_config_v3()
        complaint_ticket = Ticket("TK_COMPLAINT", "我要投诉客服态度", "zh-CN", None)
        result = match_exact(complaint_ticket, config_v3)
        self.assertEqual(result.intent_label, "general_consultation")
        self.assertEqual(result.fallback_category, "retired_label")

    def test_label_retired_without_fallback_raises(self):
        config_v3 = build_config_v3()
        from intentkit.models import IntentConfig, IntentRule

        no_escape_rules = []
        for rule in config_v3.rules:
            if rule.label == "general_consultation":
                continue
            no_escape_rules.append(
                IntentRule(
                    label=rule.label,
                    keywords=rule.keywords,
                    weight=rule.weight,
                    threshold=rule.threshold,
                    fallback_label=(
                        None if rule.label == "complaint" else rule.fallback_label
                    ),
                    batch_size=rule.batch_size,
                )
            )
        no_escape = IntentConfig.build(
            version=4,
            controlled_labels=tuple(
                label
                for label in config_v3.controlled_labels
                if label != "general_consultation"
            ),
            rules=no_escape_rules,
            default_fallback_label="general_consultation",
        )
        complaint_ticket = Ticket("TK_COMPLAINT2", "我要投诉", "zh-CN", None)
        with self.assertRaises(BizError) as context:
            match_exact(complaint_ticket, no_escape)
        self.assertEqual(context.exception.code, "E_LABEL_RETIRED")


if __name__ == "__main__":
    unittest.main()
