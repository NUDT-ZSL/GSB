from __future__ import annotations

import math
import unittest

from gsb.aggregation import (
    aggregate_batch_partial,
    materialize_groups,
    merge_partial,
    state_digest,
)
from gsb.models import AggregateSpec, ColumnBatch, QuerySpec


class AggregationTests(unittest.TestCase):
    def make_batch(self, index: int, ordinal: int) -> ColumnBatch:
        if index == 1:
            columns = {
                "k": ["b", "a", "a", "b"],
                "v": [1.0, math.nan, math.inf, 2.0],
            }
        else:
            columns = {
                "k": ["a", "b", "a"],
                "v": [3.0, -math.inf, 4.0],
            }
        return ColumnBatch(
            ordinal=ordinal,
            batch_index=index,
            row_count=len(columns["k"]),
            columns=columns,
            digest=f"digest-{index}",
            source_path=f"batch-{index}.json",
            actual_digest=f"actual-{index}",
        )

    def query(self) -> QuerySpec:
        return QuerySpec(
            query_id="q",
            group_key="k",
            aggregates=(
                AggregateSpec("sum_v", "sum", "v"),
                AggregateSpec("avg_v", "avg", "v"),
                AggregateSpec("var_v", "var", "v"),
                AggregateSpec("count_v", "count", "v"),
                AggregateSpec("rows", "count", None),
            ),
        )

    def test_exact_merge_nan_inf_and_stable_order(self) -> None:
        query = self.query()
        groups = {}
        for batch in (self.make_batch(1, 1), self.make_batch(2, 2)):
            merge_partial(groups, aggregate_batch_partial(batch, query), query)
        output = materialize_groups(groups, query)
        self.assertEqual([item["key"] for item in output], ["a", "b"])
        a = output[0]
        b = output[1]
        self.assertEqual(a["rows"], 4)
        self.assertTrue(math.isnan(a["aggregates"]["sum_v"]["value"]))
        self.assertEqual(a["aggregates"]["count_v"]["value"], 4)
        self.assertTrue(math.isinf(b["aggregates"]["sum_v"]["value"]))
        self.assertTrue(math.isinf(b["aggregates"]["avg_v"]["value"]))
        self.assertEqual(b["aggregates"]["rows"]["value"], 3)

    def test_repeat_is_bitwise_stable(self) -> None:
        query = self.query()
        first = {}
        second = {}
        for batch in (self.make_batch(1, 1), self.make_batch(2, 2)):
            merge_partial(first, aggregate_batch_partial(batch, query), query)
        for batch in (self.make_batch(1, 1), self.make_batch(2, 2)):
            merge_partial(second, aggregate_batch_partial(batch, query), query)
        self.assertEqual(state_digest(first), state_digest(second))

    def test_group_key_nan_is_rejected_by_loader_not_here(self) -> None:
        with self.assertRaises(ValueError):
            from gsb.util import group_key_token

            group_key_token(math.nan)


if __name__ == "__main__":
    unittest.main()

