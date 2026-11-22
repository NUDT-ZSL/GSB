import unittest

from intentkit.generator import build_config_v1, generate_tickets
from intentkit.matcher import (
    build_keyword_entries,
    build_signature_buckets,
    match_approximate,
    match_exact,
)
from intentkit.models import Ticket


class MatcherTests(unittest.TestCase):
    def setUp(self):
        self.config = build_config_v1()
        self.entries = build_keyword_entries(self.config)
        self.buckets = build_signature_buckets(self.entries)

    def test_deterministic_outputs_byte_identical(self):
        tickets = generate_tickets(count=40)
        first = [match_exact(ticket, self.config).to_dict() for ticket in tickets]
        second = [match_exact(ticket, self.config).to_dict() for ticket in tickets]
        self.assertEqual(first, second)

    def test_offsets_are_codepoint_based(self):
        ticket = Ticket("T1", "我要申请退款和发票", "zh-CN", None)
        result = match_exact(ticket, self.config)
        hit_map = {hit.keyword: (hit.start, hit.end) for hit in result.hits}
        self.assertEqual(hit_map["申请退款"], (2, 6))
        self.assertEqual(hit_map["发票"], (7, 9))
        for hit in result.hits:
            self.assertEqual(
                ticket.query_text[hit.start:hit.end],
                hit.keyword,
            )

    def test_longest_keyword_wins_over_short(self):
        ticket = Ticket("T2", "请处理申请退款", "zh-CN", None)
        result = match_exact(ticket, self.config)
        keywords = [hit.keyword for hit in result.hits]
        self.assertIn("申请退款", keywords)
        self.assertNotIn("退款", keywords)

    def test_exact_and_approximate_agree(self):
        tickets = generate_tickets(count=64)
        for ticket in tickets:
            exact = match_exact(ticket, self.config, self.entries)
            approx = match_approximate(
                ticket, self.config, self.entries, self.buckets
            )
            self.assertEqual(exact.intent_label, approx.intent_label, ticket.ticket_id)
            self.assertEqual(
                [hit.to_dict() for hit in exact.hits],
                [hit.to_dict() for hit in approx.hits],
            )
            self.assertEqual(exact.confidence, approx.confidence)
            self.assertIsNotNone(approx.prune_stats)
            self.assertGreaterEqual(approx.prune_stats.pruned_by_first_char, 0)

    def test_pruning_stats_record_buckets(self):
        ticket = Ticket("T3", "查询物流信息和快递单号", "zh-CN", None)
        approx = match_approximate(ticket, self.config, self.entries, self.buckets)
        self.assertTrue(approx.prune_stats.bucket_observations)
        for observation in approx.prune_stats.bucket_observations:
            self.assertIn("bucket_id", observation)
            self.assertGreaterEqual(observation["bucket_candidates"], 0)

    def test_no_keyword_uses_default_fallback(self):
        ticket = Ticket("T4", "今天天气好像还不错", "zh-CN", None)
        result = match_exact(ticket, self.config)
        self.assertEqual(result.intent_label, "general_consultation")
        self.assertEqual(result.fallback_category, "no_keyword_hit")


if __name__ == "__main__":
    unittest.main()
