import json
import os
import unittest

from ranker.config import load_config, load_groups
from ranker.fixedpoint import FixedSpec
from ranker.fusion import fuse_request
from ranker.loader import load_candidates
from ranker.normalize import normalize_request
from ranker.pipeline import prepare_candidates, run_request
from ranker.cache import CacheBook
from ranker.rerank import rerank_request
from ranker.diagnostics import run_pipeline_at_spec

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PARAMS = os.path.join(ROOT, "params")


class PipelineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = load_config(os.path.join(PARAMS, "rank_config.json"))
        cls.groups = load_groups(os.path.join(PARAMS, "feature_groups.json"))
        from ranker.config import group_indices
        cls.groups_resolved = group_indices(cls.groups, cls.config["feature_names"])
        recall = [n for n in cls.config["feature_names"] if n.startswith("recall_")]
        dense = [n for n in cls.config["feature_names"] if n.startswith("dense_")]
        sparse = [n for n in cls.config["feature_names"] if n.startswith("sparse_")]
        cls.requests, cls.failures, _ = load_candidates(
            os.path.join(PARAMS, "candidates.jsonl"), len(dense), recall, sparse
        )

    def test_loader_collects_invalid_rows(self):
        codes = [f["code"] for f in self.failures]
        self.assertIn("E_INPUT_INVALID", codes)
        self.assertTrue(any("non-finite" in f["message"] for f in self.failures))
        self.assertTrue(any("non-negative integer" in f["message"] for f in self.failures))
        self.assertTrue(any("duplicate item_id" in f["message"] for f in self.failures))
        self.assertTrue(any("malformed JSON" in f["message"] for f in self.failures))

    def test_all_scoring_uses_integers(self):
        spec = FixedSpec(self.config["frac_bits"], self.config["levels"],
                         self.config["rounding"])
        req = prepare_candidates(
            self.requests[0], self.config["feature_names"],
            [n for n in self.config["feature_names"] if n.startswith("recall_")],
            [n for n in self.config["feature_names"] if n.startswith("dense_")],
            [n for n in self.config["feature_names"] if n.startswith("sparse_")],
        )
        norm = normalize_request(req, self.config["feature_names"],
                                 self.config["normalization"], spec)
        for row in norm["codes"]:
            self.assertTrue(all(isinstance(v, int) for v in row))
        fused = fuse_request(norm, self.config["weights"], self.config["temperature"],
                             self.config["score_clip"], spec)
        for item in fused:
            self.assertIsInstance(item["score"], int)
            self.assertTrue(all(isinstance(c, int) for c in item["contributions"]))

    def test_bit_width_changes_codes_and_ranking(self):
        req = self.requests[0]
        low = run_pipeline_at_spec(req, self.config["feature_names"], self.config,
                                   self.groups_resolved,
                                   FixedSpec(3, self.config["levels"], "half_even"))
        high = run_pipeline_at_spec(req, self.config["feature_names"], self.config,
                                    self.groups_resolved,
                                    FixedSpec(4, self.config["levels"], "half_even"))
        self.assertNotEqual(low[0]["codes"], high[0]["codes"])
        self.assertNotEqual(
            [r["item_id"] for r in low[2]["ranking"]],
            [r["item_id"] for r in high[2]["ranking"]],
        )

    def test_levels_change_integer_segment(self):
        spec9 = FixedSpec(3, 9, "half_even")
        spec4 = FixedSpec(3, 4, "half_even")
        req = prepare_candidates(
            self.requests[0], self.config["feature_names"],
            [n for n in self.config["feature_names"] if n.startswith("recall_")],
            [n for n in self.config["feature_names"] if n.startswith("dense_")],
            [n for n in self.config["feature_names"] if n.startswith("sparse_")],
        )
        codes9 = normalize_request(req, self.config["feature_names"],
                                   self.config["normalization"], spec9)["codes"]
        codes4 = normalize_request(req, self.config["feature_names"],
                                   self.config["normalization"], spec4)["codes"]
        self.assertGreater(len({v for row in codes9 for v in row}),
                           len({v for row in codes4 for v in row}))

    def test_zero_penalty_degrades_to_score_sort(self):
        cfg = dict(self.config)
        cfg["diversity_penalty"] = 0.0
        spec = FixedSpec(cfg["frac_bits"], cfg["levels"], cfg["rounding"])
        req = prepare_candidates(
            self.requests[1], cfg["feature_names"],
            [n for n in cfg["feature_names"] if n.startswith("recall_")],
            [n for n in cfg["feature_names"] if n.startswith("dense_")],
            [n for n in cfg["feature_names"] if n.startswith("sparse_")],
        )
        norm = normalize_request(req, cfg["feature_names"], cfg["normalization"], spec)
        fused = fuse_request(norm, cfg["weights"], cfg["temperature"],
                             cfg["score_clip"], spec)
        scores = [f["score"] for f in fused]
        item_ids = [c["item_id"] for c in req["candidates"]]
        result = rerank_request(item_ids, norm["codes"], scores,
                                self.groups_resolved, cfg, spec)
        expected = sorted(range(len(item_ids)), key=lambda i: (-scores[i], item_ids[i]))
        self.assertEqual(result["order"], expected)
        self.assertEqual(result["penalties"], [])

    def test_tie_break_by_item_id(self):
        spec = FixedSpec(3, 9, "half_even")
        item_ids = ["z-item", "a-item", "m-item"]
        codes = [[1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1] for _ in item_ids]
        scores = [5, 5, 5]
        cfg = dict(self.config)
        cfg["diversity_penalty"] = 0.0
        result = rerank_request(item_ids, codes, scores, self.groups_resolved, cfg, spec)
        self.assertEqual([r["item_id"] for r in result["ranking"]],
                         ["a-item", "m-item", "z-item"])


    def test_global_tie_break_includes_outside_window(self):
        spec = FixedSpec(3, 9, "half_even")
        # 10 个候选全部同分，窗口仅 3；窗口内外都必须按 item_id 字典序
        item_ids = [f"item-{i:02d}" for i in (9, 1, 5, 0, 7, 3, 8, 2, 6, 4)]
        codes = [[1] * len(self.config["feature_names"]) for _ in item_ids]
        scores = [4] * len(item_ids)
        cfg = dict(self.config)
        cfg["rerank_window"] = 3
        cfg["diversity_penalty"] = 0.0
        result = rerank_request(item_ids, codes, scores, self.groups_resolved, cfg, spec)
        ordered_ids = [row["item_id"] for row in result["ranking"]]
        self.assertEqual(ordered_ids, sorted(item_ids))

    def test_determinism_repeated_runs(self):
        spec = FixedSpec(self.config["frac_bits"], self.config["levels"],
                         self.config["rounding"])
        book1, book2 = CacheBook(), CacheBook()
        artifact1, _ = run_request(self.requests[0], self.config, self.groups_resolved,
                                   self.groups["hash"], spec, [], book1, None)
        artifact2, _ = run_request(self.requests[0], self.config, self.groups_resolved,
                                   self.groups["hash"], spec, [], book2, None)
        self.assertEqual(
            json.dumps(artifact1, sort_keys=True),
            json.dumps(artifact2, sort_keys=True),
        )

    def test_diagnostics_flags_boundary_candidates(self):
        spec = FixedSpec(3, self.config["levels"], "half_even")
        from ranker.diagnostics import diagnose
        diag = diagnose(self.requests[0], self.config["feature_names"], self.config,
                        self.groups_resolved, spec, FixedSpec(4, self.config["levels"], "half_even"))
        self.assertTrue(diag["boundary_sensitive"])
        for row in diag["per_candidate_stage_diffs"]:
            self.assertIn("normalization", row["diffs"])
            self.assertIn("fusion", row["diffs"])
            self.assertIn("clip", row["diffs"])
            self.assertIn("rerank_penalty", row["diffs"])
        ranks = [d["rank"] for d in diag["rank_diffs_sorted"]]
        self.assertEqual(ranks, sorted(ranks))


if __name__ == "__main__":
    unittest.main()
