import tempfile
import unittest
from pathlib import Path

from intentkit.cache import ContentStore
from intentkit.errors import BizError
from intentkit.generator import build_config_v1, generate_tickets
from intentkit.lineage import replay_all, replay_one
from intentkit.matcher import match_exact
from intentkit.models import IntentConfig, IntentRule, Ticket
from intentkit.validation import validate_config, validate_tickets


class ValidationLineageTests(unittest.TestCase):
    def setUp(self):
        self.config = build_config_v1()
        self.tickets = generate_tickets(count=32)

    def test_duplicate_ticket_and_empty_query(self):
        bad = list(self.tickets[:3]) + [self.tickets[0]]
        bad.append(Ticket("TK_EMPTY", "", "zh-CN", None))
        issues = validate_tickets(bad)
        codes = {issue.code for issue in issues}
        self.assertIn("E_DUPLICATE_TICKET_ID", codes)
        self.assertIn("E_EMPTY_QUERY", codes)

    def test_bad_threshold_and_empty_keyword_and_label(self):
        bad_config = IntentConfig.build(
            version=100,
            controlled_labels=self.config.controlled_labels,
            rules=[
                IntentRule(
                    label="ghost_label",
                    keywords=("", "退款"),
                    weight=5,
                    threshold=1.5,
                    fallback_label=None,
                    batch_size=16,
                )
            ],
            default_fallback_label="general_consultation",
        )
        issues = validate_config(bad_config)
        codes = {issue.code for issue in issues}
        self.assertIn("E_THRESHOLD_RANGE", codes)
        self.assertIn("E_EMPTY_KEYWORD", codes)
        self.assertIn("E_LABEL_NOT_CONTROLLED", codes)

    def test_replay_passes_and_detects_tampering(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ContentStore(Path(tmp) / "cache")
            result = match_exact(self.tickets[0], self.config)
            store.put(result.cache_key, result.to_dict())
            replayed = replay_one(
                self.tickets[0], self.config, result.to_dict(), store=store
            )
            self.assertEqual(replayed["intent_label"], result.intent_label)

            tampered = result.to_dict()
            tampered["intent_label"] = "complaint"
            with self.assertRaises(BizError) as context:
                replay_one(self.tickets[0], self.config, tampered, store=store)
            self.assertEqual(context.exception.code, "E_LINEAGE_BROKEN")

    def test_replay_all_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ContentStore(Path(tmp) / "cache")
            records = []
            for ticket in self.tickets:
                result = match_exact(ticket, self.config)
                store.put(result.cache_key, result.to_dict())
                records.append(result.to_dict())
            report = replay_all(self.tickets, self.config, records, store=store)
            self.assertTrue(report.to_dict()["all_passed"])
            self.assertEqual(report.passed_total, len(self.tickets))


if __name__ == "__main__":
    unittest.main()
