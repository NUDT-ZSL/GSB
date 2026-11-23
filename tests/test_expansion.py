import unittest

from xrel.config import ExpandConfig
from xrel.errors import ConfidenceOutOfRange, DirectionMissing
from xrel.expansion import expand_query
from xrel.model import BridgeResource, Query


def bridge_with(entries, directions, version=1):
    return BridgeResource(version=version, directions=directions, entries=entries)


class ExpansionTests(unittest.TestCase):
    def setUp(self):
        self.entries = {
            ("en", "en:cat"): [
                ("de", "de:katze", 0.9),
                ("zh", "zh:猫", 0.8),
                ("de", "de:kater", 0.9),
            ],
            ("en", "en:dog"): [
                ("de", "de:hund", 0.5),
            ],
        }
        self.directions = [("en", "de"), ("en", "zh")]
        self.bridge = bridge_with(self.entries, self.directions)
        self.query = Query("q1", "en", ["en:cat", "en:dog"], top_k=5)

    def test_deterministic_ordering_confidence_then_term(self):
        result = expand_query(self.query, self.bridge, ExpandConfig(min_confidence=0.0))
        terms = [(c.target_term, c.confidence) for c in result.candidates]
        self.assertEqual(terms[0], ("de:kater", 0.9))
        # Equal confidence resolves by target term lexicographic order.
        self.assertEqual(terms[1], ("de:katze", 0.9))
        self.assertEqual(terms[2], ("zh:猫", 0.8))

    def test_byte_identical_repeated_expansion(self):
        first = expand_query(self.query, self.bridge, ExpandConfig()).to_dict()
        second = expand_query(self.query, self.bridge, ExpandConfig()).to_dict()
        self.assertEqual(first, second)

    def test_max_candidates_and_min_confidence_constraints(self):
        result = expand_query(
            self.query, self.bridge, ExpandConfig(max_candidates=2, min_confidence=0.6)
        )
        self.assertEqual(len(result.candidates), 2)
        self.assertTrue(all(c.confidence >= 0.6 for c in result.candidates))

    def test_direction_missing_returns_structured_code(self):
        broken = bridge_with(self.entries, [("en", "zh")])
        with self.assertRaises(DirectionMissing) as ctx:
            expand_query(
                self.query,
                broken,
                ExpandConfig(min_confidence=0.0),
                target_languages=["de", "zh"],
            )
        self.assertEqual(ctx.exception.code, "E_DIRECTION_MISSING")
        self.assertEqual(ctx.exception.context["query_id"], "q1")
        self.assertEqual(ctx.exception.context["term"], "en:cat")

    def test_confidence_out_of_range_returns_structured_code(self):
        entries = {("en", "en:cat"): [("de", "de:x", 1.4)]}
        broken = bridge_with(entries, [("en", "de")])
        with self.assertRaises(ConfidenceOutOfRange) as ctx:
            expand_query(
                Query("q2", "en", ["en:cat"], 5), broken, ExpandConfig(min_confidence=0.0)
            )
        self.assertEqual(ctx.exception.code, "E_CONFIDENCE_OUT_OF_RANGE")
        self.assertEqual(ctx.exception.context["term"], "en:cat")


if __name__ == "__main__":
    unittest.main()
