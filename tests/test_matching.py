"""匹配正确性、确定性与聚合一致性测试。"""

import unittest

from detwb.canon import canonical_json
from detwb.dag import Pipeline
from detwb.matching import greedy_assign, iou_matrix, match_sample
from detwb.samples import gen_manifest
from detwb.vocab import VocabRegistry
from tests.helpers import make_vocab


def _registry():
    reg = VocabRegistry()
    reg.load(make_vocab())
    return reg


def _config(threshold=0.5):
    return {"iou_threshold": threshold, "fallback_class_id": None,
            "limits": {"max_images": 1000, "max_iou_comparisons": 10000000,
                       "max_resident_candidates": 100000,
                       "degrade_allowed": False}}


class TestGreedy(unittest.TestCase):
    def test_score_desc_and_coordinate_tiebreak(self):
        cands = [
            {"cand_idx": 0, "box": [10.0, 0.0, 20.0, 10.0], "score": 0.9},
            {"cand_idx": 1, "box": [0.0, 0.0, 10.0, 10.0], "score": 0.9},
            {"cand_idx": 2, "box": [0.0, 0.0, 10.0, 10.0], "score": 0.8},
        ]
        gts = [{"gt_idx": 0, "box": [0.0, 0.0, 10.0, 10.0]},
               {"gt_idx": 1, "box": [10.0, 0.0, 20.0, 10.0]}]
        ious, _ = iou_matrix([c["box"] for c in cands],
                             [g["box"] for g in gts])
        assign = greedy_assign(cands, gts, ious, 0.5)
        # 同分时坐标字典序小者（cand 1）先指派，抢走 gt0
        self.assertEqual(assign[1][0], 0)
        self.assertEqual(assign[0][0], 1)
        self.assertEqual(assign[2][0], -1)

    def test_match_sample_sets(self):
        sample = {
            "sample_id": "sX", "seed": 1, "index": 0,
            "image": {"sha256": "x"},
            "candidates": [
                {"cand_idx": 0, "box": [0.0, 0.0, 10.0, 10.0],
                 "score": 0.9, "class_id": 1},
                {"cand_idx": 1, "box": [40.0, 40.0, 50.0, 50.0],
                 "score": 0.3, "class_id": 1},
            ],
            "gt": [{"gt_idx": 0, "box": [0.5, 0.5, 10.5, 10.5],
                   "label": 1, "difficulty": "easy", "occluded": False}],
        }
        records, stats = match_sample(sample, 0.5)
        self.assertEqual(stats["tp"], [0])
        self.assertEqual(stats["fp"], [1])
        self.assertEqual(stats["fn"], [])
        tp = [r for r in records if r["decision"] == "TP"][0]
        self.assertEqual(tp["gt_idx"], 0)
        self.assertGreater(tp["iou"], 0.5)


class TestPipelineDeterminism(unittest.TestCase):
    def test_byte_identical_runs(self):
        samples = gen_manifest(777, 6, 3)
        out1 = Pipeline(_config(), _registry()).run(samples)
        out2 = Pipeline(_config(), _registry()).run(samples)
        self.assertEqual(canonical_json(out1), canonical_json(out2))

    def test_aggregate_gt_conservation(self):
        samples = gen_manifest(777, 6, 3)
        result = Pipeline(_config(), _registry()).run(samples)
        by_class = result["aggregates"]["by_class"]
        gt_per_class = {}
        for s in samples:
            for g in s["gt"]:
                gt_per_class[g["label"]] = gt_per_class.get(g["label"], 0) + 1
        for cid, n in gt_per_class.items():
            bucket = by_class[str(cid)]
            self.assertEqual(bucket["tp"] + bucket["fn"], n)

    def test_threshold_monotonic_tp(self):
        samples = gen_manifest(777, 6, 3)
        lo = Pipeline(_config(0.3), _registry()).run(samples)
        hi = Pipeline(_config(0.8), _registry()).run(samples)
        tp_lo = sum(len(s["tp"]) for s in lo["stats"].values())
        tp_hi = sum(len(s["tp"]) for s in hi["stats"].values())
        self.assertGreaterEqual(tp_lo, tp_hi)


if __name__ == "__main__":
    unittest.main()
