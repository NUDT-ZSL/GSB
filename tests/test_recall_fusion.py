"""要求 1/2：四路召回打分、平局规则、跨路对齐去重与可重算明细。"""

from __future__ import annotations

from sr_pipeline.config import PipelineConfig
from sr_pipeline.fusion import fuse
from sr_pipeline.index import build_index
from sr_pipeline.precision import build_ops
from sr_pipeline.recall import run_all_routes
from sr_pipeline.selfcheck import check_recomputable, check_route_doc_sets

from ._base import SampleCase


class RecallFusionTest(SampleCase):
    def _run(self, config: PipelineConfig | None = None):
        config = config or self.config
        ops = build_ops(config.precision)
        index = build_index(
            self.docs, self.queries, self.term_weights, config, ops
        )
        all_route_results = {}
        all_fusion = {}
        for query in self.queries:
            route_results = run_all_routes(query, index, config, ops)
            check_route_doc_sets(index, route_results)
            result = fuse(query, route_results, config, ops)
            check_recomputable(result)
            all_route_results[query["query_id"]] = route_results
            all_fusion[query["query_id"]] = result
        return index, all_route_results, all_fusion

    def test_four_routes_present_and_deterministic(self) -> None:
        _, route_results, fusion = self._run()
        first_query = self.queries[0]["query_id"]
        routes = {result.route for result in route_results[first_query]}
        self.assertEqual(
            routes, {"inverted", "prefix", "category_hot", "vector_ann"}
        )
        # 再跑一次，结果逐候选一致
        _, _, fusion_again = self._run()
        for query_id, result in fusion.items():
            again = fusion_again[query_id]
            self.assertEqual(
                [c.doc_id for c in result.candidates],
                [c.doc_id for c in again.candidates],
            )
            self.assertEqual(
                [c.final_score for c in result.candidates],
                [c.final_score for c in again.candidates],
            )

    def test_tie_break_by_doc_id_ascending(self) -> None:
        # 类别热门路存在大量同分候选，必须按 doc_id 升序
        ops = build_ops(self.config.precision)
        index = build_index(
            self.docs, self.queries, self.term_weights, self.config, ops
        )
        from sr_pipeline.recall import category_hot_recall

        route_config = self.config.route("category_hot")
        result = category_hot_recall(
            self.queries[0], index, route_config, ops
        )
        raws = [ops.to_float(hit.raw) for hit in result.hits]
        for previous, current in zip(raws, raws[1:]):
            self.assertGreaterEqual(previous, current)
        same_score = [
            hit.doc_id for hit in result.hits
            if ops.to_float(hit.raw) == ops.to_float(result.hits[0].raw)
        ]
        self.assertEqual(same_score, sorted(same_score))

    def test_contributions_recompute_final_score(self) -> None:
        _, _, fusion = self._run()
        for result in fusion.values():
            for candidate in result.candidates:
                expected = sum(
                    item.weighted_float for item in candidate.contributions
                )
                self.assertAlmostEqual(
                    expected, candidate.final_score_float, places=12
                )
                # 每条贡献四要素齐全
                for item in candidate.contributions:
                    self.assertGreater(item.weight, 0.0)
                    self.assertTrue(item.details is not None)

    def test_dedup_no_duplicate_and_owner_recorded(self) -> None:
        _, _, fusion = self._run()
        for result in fusion.values():
            doc_ids = [c.doc_id for c in result.candidates]
            self.assertEqual(len(doc_ids), len(set(doc_ids)))
            for candidate in result.candidates:
                owners = {item.route for item in candidate.contributions}
                self.assertIn(candidate.owner_route, owners)

    def test_first_hit_dedup_policy(self) -> None:
        from dataclasses import replace

        config = replace(
            self.config,
            fusion=replace(self.config.fusion, dedup_policy="first_hit"),
        )
        _, _, fusion = self._run(config)
        route_order = config.fusion.route_order
        for result in fusion.values():
            for candidate in result.candidates:
                hit_routes = [
                    item.route for item in candidate.contributions
                ]
                expected_owner = next(
                    route for route in route_order if route in hit_routes
                )
                self.assertEqual(candidate.owner_route, expected_owner)

    def test_highest_norm_owner_is_max(self) -> None:
        _, _, fusion = self._run()
        for result in fusion.values():
            for candidate in result.candidates:
                max_route = max(
                    candidate.contributions,
                    key=lambda item: item.normalized_float,
                ).route
                self.assertEqual(candidate.owner_route, max_route)
