from __future__ import annotations

import unittest

from water_quality.predicates import (
    Predicate,
    PredicateGroup,
    PredicateSet,
    QuerySpec,
    kleene_and,
    kleene_or,
    parse_reading_groups,
    truth_label,
)


class KleeneLogicTests(unittest.TestCase):
    def test_kleene_truth_tables(self) -> None:
        self.assertIs(kleene_and([True, None]), None)
        self.assertIs(kleene_and([False, None]), False)
        self.assertIs(kleene_and([True, True]), True)
        self.assertIs(kleene_or([False, None]), None)
        self.assertIs(kleene_or([True, None]), True)
        self.assertIs(kleene_or([False, False]), False)
        self.assertEqual(truth_label(None), "UNKNOWN")

    def test_null_comparison_is_unknown_not_true_or_false(self) -> None:
        predicate = Predicate(
            predicate_id="P1",
            source="reading",
            field="residual_chlorine",
            operator="<",
            value=0.2,
        )
        value, null_caused = predicate.evaluate({"residual_chlorine": None})
        self.assertIsNone(value)
        self.assertTrue(null_caused)
        sql = predicate.sql()
        self.assertIn("IS NOT NULL", sql)
        self.assertNotIn("COALESCE", sql)

    def test_or_group_can_be_true_even_when_one_side_unknown(self) -> None:
        predicates = PredicateSet(
            (
                PredicateGroup(
                    (
                        Predicate(
                            "P1", "reading", "ph", "<", 6.5
                        ),
                    )
                ),
                PredicateGroup(
                    (
                        Predicate(
                            "P2", "reading", "turbidity", ">", 1.0
                        ),
                    )
                ),
            )
        )
        result, unknowns = predicates.evaluate(
            {"ph": None, "turbidity": 2.0}
        )
        self.assertIs(result, True)
        self.assertEqual([item[0].predicate_id for item in unknowns], ["P1"])
        self.assertIn(" OR ", predicates.sql())

    def test_is_null_is_not_unknown(self) -> None:
        predicate = Predicate(
            "P", "reading", "turbidity", "IS NULL", None
        )
        self.assertEqual(predicate.evaluate({"turbidity": None}), (True, False))

    def test_query_parser_accepts_dnf_groups(self) -> None:
        query = QuerySpec.from_dict(
            {
                "query_id": "QX",
                "join_type": "left",
                "reading_predicate_groups": [
                    [{"field": "ph", "operator": "<", "value": 6.5}],
                    [{"field": "ph", "operator": ">", "value": 8.5}],
                ],
            }
        )
        self.assertEqual(len(query.reading_predicates.groups), 2)
        self.assertEqual(query.join_type, "left")


if __name__ == "__main__":
    unittest.main()

