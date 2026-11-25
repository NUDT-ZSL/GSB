"""要求 8：结构化输入校验业务码与字段定位。"""

from __future__ import annotations

import copy
from dataclasses import replace

from sr_pipeline.errors import BusinessError, ErrorCode
from sr_pipeline.validate import (
    validate_config,
    validate_documents,
    validate_queries,
    validate_term_weights,
)

from ._base import SampleCase


class ValidationTest(SampleCase):
    def test_duplicate_query_id(self) -> None:
        queries = copy.deepcopy(self.queries)
        queries[1]["query_id"] = queries[0]["query_id"]
        with self.assertRaises(BusinessError) as caught:
            validate_queries(queries, self.config.vocab_size)
        self.assertEqual(caught.exception.code, ErrorCode.DUPLICATE_QUERY_ID)
        self.assertIn("queries[1].query_id", caught.exception.field_path)

    def test_doc_term_out_of_range(self) -> None:
        docs = copy.deepcopy(self.docs)
        docs[2]["terms"][0] = self.config.vocab_size + 5
        with self.assertRaises(BusinessError) as caught:
            validate_documents(docs, self.config.vocab_size)
        self.assertEqual(
            caught.exception.code, ErrorCode.DOC_TERM_OUT_OF_RANGE
        )
        self.assertIn("terms[0]", caught.exception.field_path)

    def test_non_positive_route_weight(self) -> None:
        routes = list(self.config.routes)
        routes[0] = replace(routes[0], weight=0.0)
        bad = replace(self.config, routes=tuple(routes))
        with self.assertRaises(BusinessError) as caught:
            validate_config(bad)
        self.assertEqual(caught.exception.code, ErrorCode.INVALID_WEIGHT)
        self.assertIn("weight", caught.exception.field_path)

    def test_non_positive_budget(self) -> None:
        bad = replace(
            self.config,
            scheduler=replace(self.config.scheduler, batch_size=-1),
        )
        with self.assertRaises(BusinessError) as caught:
            validate_config(bad)
        self.assertEqual(caught.exception.code, ErrorCode.INVALID_BUDGET)
        self.assertIn("batch_size", caught.exception.field_path)

    def test_term_weight_non_positive(self) -> None:
        weights = dict(self.term_weights)
        weights["7"] = 0.0
        with self.assertRaises(BusinessError) as caught:
            validate_term_weights(weights, self.config.vocab_size)
        self.assertEqual(caught.exception.code, ErrorCode.INVALID_WEIGHT)

    def test_missing_term_weight(self) -> None:
        weights = dict(self.term_weights)
        del weights["3"]
        with self.assertRaises(BusinessError) as caught:
            validate_term_weights(weights, self.config.vocab_size)
        self.assertEqual(caught.exception.code, ErrorCode.MISSING_FIELD)
