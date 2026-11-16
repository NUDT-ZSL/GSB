import unittest

from config_audit.comparison import (
    CONSISTENT,
    DEVIATING,
    INDISTINGUISHABLE,
    TOL_ABS,
    TOL_REL,
    Tolerance,
    compare_pair,
)
from config_audit.fixedpoint import Grid
from config_audit.models import QuantizedItem


def _qi(node, q, down=0, up=0):
    return QuantizedItem(
        node_id=node, key="k", unit="ms", source="manual",
        qvalue=q, down_error=down, up_error=up, location=f"{node}/k",
    )


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        self.grid = Grid.create(0)
        self.tol = Tolerance.create(self.grid, "5", 0)

    def test_inclusive_boundary_is_consistent(self):
        # deviation exactly equals tolerance -> inclusive -> consistent
        v = compare_pair(_qi("a", 0), _qi("b", 5), self.tol)
        self.assertTrue(v.within_tolerance)
        self.assertEqual(v.verdict, CONSISTENT)
        self.assertEqual(v.triggered_tolerance, TOL_ABS)

    def test_just_outside_is_deviating(self):
        v = compare_pair(_qi("a", 0), _qi("b", 6), self.tol)
        self.assertFalse(v.within_tolerance)
        self.assertEqual(v.verdict, DEVIATING)

    def test_relative_tolerance_dominates(self):
        tol = Tolerance.create(self.grid, "1", 10_000)  # 1%
        v = compare_pair(_qi("a", 1000), _qi("b", 1005), tol)
        # relative 1% of 1005 = ceil(10.05) = 11 > abs 1
        self.assertEqual(v.rel_tolerance, 11)
        self.assertEqual(v.triggered_tolerance, TOL_REL)
        self.assertEqual(v.verdict, CONSISTENT)

    def test_absolute_wins_on_tie(self):
        tol = Tolerance.create(self.grid, "10", 10_000)
        v = compare_pair(_qi("a", 1000), _qi("b", 1010), tol)
        # relative = ceil(10.1)=11, abs=10 -> relative strictly larger
        self.assertEqual(v.triggered_tolerance, TOL_REL)
        tol_tie = Tolerance.create(self.grid, "11", 10_000)
        v_tie = compare_pair(_qi("a", 1000), _qi("b", 1010), tol_tie)
        self.assertEqual(v_tie.triggered_tolerance, TOL_ABS)

    def test_error_bound_dominance(self):
        # deviation 2, but each side carries uncertainty 1 -> indistinguishable
        v = compare_pair(_qi("a", 100, up=1), _qi("b", 102, down=1), self.tol)
        self.assertTrue(v.error_bound_dominated)
        self.assertEqual(v.verdict, INDISTINGUISHABLE)
        self.assertEqual(v.uncertainty, 2)

    def test_exact_zero_error_match_is_consistent(self):
        v = compare_pair(_qi("a", 5), _qi("b", 5), self.tol)
        self.assertFalse(v.error_bound_dominated)
        self.assertEqual(v.verdict, CONSISTENT)


if __name__ == "__main__":
    unittest.main()
