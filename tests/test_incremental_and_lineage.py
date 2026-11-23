from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from meeting_retrieval.errors import BusinessError, E_LINEAGE_BROKEN
from meeting_retrieval.service import MeetingRetrievalApp


class IncrementalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.ws = Path(self.temp.name) / "ws"
        self.app = MeetingRetrievalApp(self.ws)
        self.initial = self.app.run()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _node_pairs(self, output, node: str) -> set:
        return {item["pair_id"] for item in output.recomputed_nodes if item["node"] == node}

    def test_text_edit_only_invalidates_one_pair_suffix(self) -> None:
        old_keys = self.app.engine.manifest()["nodes"]["text"]["p01"]["cache_key"]
        output = self.app.update_text("p02", "预算 风险 讨论 安全 方案 验证 完成")
        self.assertEqual(output.affected["decode"], [])
        self.assertEqual(output.affected["visual"], [])
        self.assertEqual(output.affected["text"], ["p02"])
        self.assertEqual(output.affected["align"], ["p02"])
        self.assertEqual(output.affected["pair_ids"], ["p02"])
        self.assertEqual(self._node_pairs(output, "text"), {"p02"})
        self.assertEqual(self._node_pairs(output, "align"), {"p02"})
        self.assertEqual(self._node_pairs(output, "ranking"), {"__global__"})
        new_keys = self.app.engine.manifest()["nodes"]["text"]["p01"]["cache_key"]
        self.assertEqual(old_keys, new_keys)
        self.assertNotIn("p01", output.affected["text"])

    def test_seed_replacement_only_invalidates_the_changed_image(self) -> None:
        old_text_key = self.app.engine.manifest()["nodes"]["text"]["p03"]["cache_key"]
        old_visual_key = self.app.engine.manifest()["nodes"]["visual"]["p04"]["cache_key"]
        output = self.app.update_seed("p03", 9999)
        self.assertEqual(output.affected["decode"], ["p03"])
        self.assertEqual(output.affected["visual"], ["p03"])
        self.assertEqual(output.affected["align"], ["p03"])
        self.assertEqual(output.affected["text"], [])
        self.assertEqual(output.affected["pair_ids"], ["p03"])
        self.assertEqual(self.app.engine.manifest()["nodes"]["text"]["p03"]["cache_key"], old_text_key)
        self.assertEqual(self.app.engine.manifest()["nodes"]["visual"]["p04"]["cache_key"], old_visual_key)

    def test_grid_change_reuses_decodes_and_text_but_recomputes_visual_suffix(self) -> None:
        output = self.app.set_feature_params(tile_rows=2)
        self.assertEqual(output.affected["decode"], [])
        self.assertEqual(output.affected["text"], [])
        self.assertEqual(set(output.affected["visual"]), set(self.app.status()["pair_ids"]))
        self.assertEqual(set(output.affected["align"]), set(self.app.status()["pair_ids"]))
        recomputed_nodes = {item["node"] for item in output.recomputed_nodes}
        self.assertNotIn("decode", recomputed_nodes)
        self.assertNotIn("text", recomputed_nodes)
        self.assertIn("visual", recomputed_nodes)
        self.assertIn("align", recomputed_nodes)
        self.assertIn("ranking", recomputed_nodes)

    def test_threshold_and_top_k_only_recompute_ranking(self) -> None:
        output = self.app.set_retrieval_params(min_score=0.4, top_k=3)
        self.assertEqual(output.affected["pair_ids"], [])
        self.assertEqual(output.affected["ranking"], ["__global__"])
        self.assertEqual({item["node"] for item in output.recomputed_nodes}, {"ranking"})
        self.assertEqual(len(output.ranking["hits"]), 3)
        self.assertTrue(all(float(hit["score"]) >= 0.4 for hit in output.ranking["hits"]))

    def test_vocabulary_change_reuses_decode_and_visual(self) -> None:
        terms = self.app.load_vocabulary().terms
        output = self.app.set_vocabulary(terms + ["额外"])
        self.assertEqual(output.affected["decode"], [])
        self.assertEqual(output.affected["visual"], [])
        self.assertEqual(set(output.affected["text"]), set(self.app.status()["pair_ids"]))
        self.assertEqual(set(output.affected["align"]), set(self.app.status()["pair_ids"]))

    def test_trace_and_replay_preserve_score_and_order(self) -> None:
        hit_id = self.initial.ranking["hits"][0]["hit_id"]
        traced = self.app.trace_hit(hit_id)
        path_nodes = [item["node"] for item in traced["hit"]["source_node_path"]]
        self.assertEqual(path_nodes, ["decode", "visual", "text", "align", "ranking"])
        self.assertIn("vocab_version", traced["hit"])
        self.assertIn("feature_params_version", traced["hit"])
        replayed = self.app.replay_hit(hit_id)
        self.assertTrue(replayed["same_score"])
        self.assertTrue(replayed["same_rank"])
        self.assertEqual(replayed["score"], replayed["recomputed_score"])
        self.assertEqual(
            [(item["rank"], item["pair_id"]) for item in replayed["ranking_order"]],
            [(item["rank"], item["pair_id"]) for item in self.initial.ranking["hits"]],
        )

    def test_old_hit_can_replay_when_only_unrelated_data_changed(self) -> None:
        hit_id = self.initial.ranking["hits"][0]["hit_id"]
        self.app.update_text("p02", "预算 风险 讨论 安全 方案 验证 完成")
        replayed = self.app.replay_hit(hit_id)
        self.assertEqual(replayed["hit_id"], hit_id)

    def test_feature_version_change_breaks_lineage(self) -> None:
        hit_id = self.initial.ranking["hits"][0]["hit_id"]
        self.app.set_feature_params(hist_bins=5)
        with self.assertRaises(BusinessError) as raised:
            self.app.replay_hit(hit_id)
        self.assertEqual(raised.exception.code, E_LINEAGE_BROKEN)
        error = raised.exception.to_dict()["error"]
        self.assertEqual(
            error["details"]["earliest_invalid_version"]["kind"],
            "feature_params",
        )

    def test_vocab_version_change_reports_earliest_invalid_vocab(self) -> None:
        hit_id = self.initial.ranking["hits"][0]["hit_id"]
        terms = self.app.load_vocabulary().terms
        self.app.set_vocabulary(terms + ["额外"])
        with self.assertRaises(BusinessError) as raised:
            self.app.replay_hit(hit_id)
        self.assertEqual(raised.exception.code, E_LINEAGE_BROKEN)
        details = raised.exception.to_dict()["error"]["details"]
        self.assertEqual(details["earliest_invalid_version"]["kind"], "vocab")

    def test_retrieval_snapshot_version_increases_after_ranking_change(self) -> None:
        first = self.app.engine.snapshots()["registries"]["retrieval_result"]["version"]
        self.app.set_retrieval_params(top_k=3)
        second = self.app.engine.snapshots()["registries"]["retrieval_result"]["version"]
        self.assertGreater(second, first)


if __name__ == "__main__":
    unittest.main()
