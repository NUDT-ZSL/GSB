from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np

from meeting_retrieval.canonical import canonical_json_bytes
from meeting_retrieval.models import FeatureConfig, RetrievalConfig, SampleRecord
from meeting_retrieval.retrieval import align_from_artifacts, build_ranking
from meeting_retrieval.service import MeetingRetrievalApp
from meeting_retrieval.text import embed_text
from meeting_retrieval.vision import decode_frame, extract_visual_features


class DeterminismTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.app = MeetingRetrievalApp(Path(self.temp.name) / "ws")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_decode_features_and_text_are_byte_deterministic(self) -> None:
        config = self.app.load_config().features
        rect = (16, 14, 176, 104)
        first = decode_frame(12345, config.canvas_shape, rect, config.texture_cell)
        second = decode_frame(12345, config.canvas_shape, rect, config.texture_cell)
        self.assertEqual(first.tobytes(order="C"), second.tobytes(order="C"))

        features_a = extract_visual_features(first, rect, config)
        features_b = extract_visual_features(second, rect, config)
        self.assertEqual(canonical_json_bytes(features_a), canonical_json_bytes(features_b))

        vocabulary = self.app.load_vocabulary()
        text_a = embed_text("项目进度评审 下周完成", vocabulary, 1)
        text_b = embed_text("项目进度评审 下周完成", vocabulary, 1)
        self.assertEqual(canonical_json_bytes(text_a), canonical_json_bytes(text_b))
        self.assertEqual(len(features_a["tiles"]), config.tile_rows * config.tile_cols)

    def test_alignment_tie_chooses_lowest_block_index(self) -> None:
        config = FeatureConfig(tile_rows=2, tile_cols=2, hist_bins=4)
        shape = config.canvas_shape
        canvas = np.full(shape, 100, dtype=np.uint8)
        visual = extract_visual_features(canvas, (10, 10, 180, 110), config)
        vocabulary = self.app.load_vocabulary()
        text = embed_text("项目 进度", vocabulary, 1)
        aligned = align_from_artifacts(
            visual,
            text,
            {"version": 1, "checksum": "f", "revision": 1},
            {"version": 1, "checksum": "v", "revision": 1},
        )
        scores = {item["score"] for item in aligned["tile_scores"]}
        self.assertEqual(len(scores), 1)
        self.assertEqual(aligned["best_block_index"], 0)

    def test_ranking_tie_uses_natural_pair_id_and_block_index(self) -> None:
        def alignment(name: str, block: int) -> dict:
            return {
                "score": "0.5",
                "artifact_hash": name,
                "best_block_index": block,
            }

        ranking = build_ranking(
            [
                ("p10", alignment("p10", 0)),
                ("p2", alignment("p2", 3)),
                ("p2", alignment("duplicate", 1)),
            ],
            RetrievalConfig(min_score=0.0, top_k=10),
            {"vocab_version": 1, "feature_version": 1},
        )
        # Duplicate pair entries are not expected from the engine; this test
        # focuses on the deterministic sort key itself.
        order = [(item["pair_id"], item["best_block_index"], item["rank"]) for item in ranking["hits"]]
        self.assertEqual(order[0], ("p2", 1, 1))
        self.assertEqual(order[1], ("p2", 3, 2))
        self.assertEqual(order[2], ("p10", 0, 3))

    def test_cache_reused_on_second_run(self) -> None:
        first = self.app.run()
        self.assertEqual(len(first.recomputed_nodes), 33)
        self.assertEqual(len(first.reused_nodes), 0)
        second = self.app.run()
        self.assertEqual(len(second.recomputed_nodes), 0)
        self.assertEqual(len(second.reused_nodes), 33)
        self.assertEqual(
            first.cache_key_preview["ranking"],
            second.cache_key_preview["ranking"],
        )

    def test_all_five_node_families_are_queryable(self) -> None:
        self.app.run()
        for node in ("decode", "visual", "text", "align", "ranking"):
            description = self.app.query_cache(node, "p01")
            self.assertEqual(description["node"], node)
            self.assertIn("key_components", description)
            self.assertIn("artifact_hash", description)
            self.assertTrue(description["cache_key"])


if __name__ == "__main__":
    unittest.main()
