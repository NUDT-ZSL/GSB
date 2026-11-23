import math
import unittest

from recall_fusion.config import FusionConfig
from recall_fusion.errors import ErrorCode, PipelineError
from recall_fusion.fusion import fuse
from recall_fusion.merge import merge_candidates
from recall_fusion.models import Candidate


def make_candidates():
    return {
        "inverted": [
            Candidate("d1", "inverted", 0.9),
            Candidate("d2", "inverted", 0.5),
        ],
        "prefix": [
            Candidate("d2", "prefix", 0.8),
            Candidate("d3", "prefix", 0.4),
        ],
        "category": [Candidate("d1", "category", 0.7)],
        "vector": [Candidate("d1", "vector", 0.3)],
    }


class MergeTest(unittest.TestCase):
    def test_merge_groups_all_hits_without_duplicate_doc_ids(self):
        merged = merge_candidates(make_candidates(), "highest_normalized", "q1")
        ids = [record.doc_id for record in merged]
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(len(ids), len(set(ids)))
        d1 = next(record for record in merged if record.doc_id == "d1")
        self.assertEqual(
            sorted(d1.hit_channels), ["category", "inverted", "vector"]
        )
        self.assertEqual(len(d1.hits), 3)

    def test_highest_normalized_keeps_best_raw_hit(self):
        merged = merge_candidates(make_candidates(), "highest_normalized", "q1")
        d2 = next(record for record in merged if record.doc_id == "d2")
        self.assertEqual(d2.kept_channel, "prefix")
        self.assertEqual(d2.kept_raw_score, 0.8)

    def test_first_hit_keeps_earliest_channel(self):
        merged = merge_candidates(make_candidates(), "first_hit", "q1")
        d2 = next(record for record in merged if record.doc_id == "d2")
        self.assertEqual(d2.kept_channel, "inverted")

    def test_illegal_merge_policy_raises_structured_error(self):
        with self.assertRaises(PipelineError) as ctx:
            merge_candidates(make_candidates(), "bogus", "q-42")
        err = ctx.exception
        self.assertEqual(err.code, ErrorCode.E_MERGE_POLICY)
        self.assertEqual(err.stage, "merge")
        self.assertEqual(err.query_id, "q-42")
        self.assertEqual(err.field, "merge_policy")


class FusionTest(unittest.TestCase):
    def test_final_score_recomputes_from_details(self):
        config = FusionConfig.default()
        candidates = make_candidates()
        merged = merge_candidates(candidates, "highest_normalized", "q1")
        fused = fuse(merged, candidates, config, "q1")
        for candidate in fused:
            recomputed = sum(detail.weighted_contribution for detail in candidate.details)
            self.assertTrue(
                math.isclose(candidate.final_score, recomputed, abs_tol=1e-12)
            )
            self.assertTrue(
                math.isclose(
                    candidate.final_score, candidate.recompute_final_score(),
                    abs_tol=1e-12,
                )
            )

    def test_rank_reciprocal_normalization(self):
        config = FusionConfig.default()
        config.scheduler.default_normalization = "rank_reciprocal"
        candidates = make_candidates()
        merged = merge_candidates(candidates, "first_hit", "q1")
        fused = fuse(merged, candidates, config, "q1")
        d1 = next(c for c in fused if c.doc_id == "d1")
        inv_detail = next(d for d in d1.details if d.channel == "inverted")
        self.assertAlmostEqual(inv_detail.normalized_score, 1 / 61)
        self.assertAlmostEqual(
            inv_detail.weighted_contribution, inv_detail.normalized_score
        )

    def test_illegal_normalization_raises_e_norm_invalid(self):
        config = FusionConfig.default()
        config.channels["vector"].normalization = "bogus"
        candidates = make_candidates()
        merged = merge_candidates(candidates, "first_hit", "q1")
        with self.assertRaises(PipelineError) as ctx:
            fuse(merged, candidates, config, "q-7")
        self.assertEqual(ctx.exception.code, ErrorCode.E_NORM_INVALID)
        self.assertEqual(ctx.exception.query_id, "q-7")

    def test_negative_weight_raises_e_weight_invalid(self):
        config = FusionConfig.default()
        config.channels["prefix"].weight = -0.5
        candidates = make_candidates()
        merged = merge_candidates(candidates, "first_hit", "q1")
        with self.assertRaises(PipelineError) as ctx:
            fuse(merged, candidates, config, "q-8")
        self.assertEqual(ctx.exception.code, ErrorCode.E_WEIGHT_INVALID)
        self.assertEqual(ctx.exception.query_id, "q-8")

    def test_deterministic_fusion_ordering(self):
        config = FusionConfig.default()
        candidates = make_candidates()
        merged = merge_candidates(candidates, "highest_normalized", "q1")
        first = fuse(merged, candidates, config, "q1")
        second = fuse(merged, candidates, config, "q1")
        self.assertEqual(
            [(c.doc_id, c.final_score) for c in first],
            [(c.doc_id, c.final_score) for c in second],
        )


if __name__ == "__main__":
    unittest.main()
