"""独立浮点参考实现，交叉验证定点链路在量化误差预算内。"""

import os
import unittest

from ranker.config import group_indices, load_config, load_groups
from ranker.fixedpoint import FixedSpec
from ranker.fusion import fuse_request
from ranker.loader import load_candidates
from ranker.normalize import normalize_request
from ranker.pipeline import prepare_candidates

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def float_reference(request, feature_names, methods, weights, temperature, clip):
    n = len(request["candidates"])
    matrix = []
    for cand in request["candidates"]:
        row = []
        for name in feature_names:
            if name in cand["scores"]:
                row.append(cand["scores"][name])
            elif name in cand["dense_map"]:
                row.append(cand["dense_map"][name])
            else:
                row.append(float(cand["sparse"][name]))
        matrix.append(row)
    dim = len(feature_names)
    normalized = [[0.0] * dim for _ in range(n)]
    for j in range(dim):
        col = [matrix[i][j] for i in range(n)]
        lo, hi = min(col), max(col)
        for i in range(n):
            if methods[j] == "minmax":
                normalized[i][j] = 0.0 if hi == lo else (col[i] - lo) / (hi - lo)
            else:
                mean = sum(col) / n
                var = sum((v - mean) ** 2 for v in col) / n
                std = var ** 0.5
                normalized[i][j] = 0.0 if std == 0 else (col[i] - mean) / std
    scores = []
    for i in range(n):
        value = sum(normalized[i][j] * weights[j] for j in range(dim)) / temperature
        scores.append(min(max(value, clip[0]), clip[1]))
    return scores


class FloatCrossCheckTest(unittest.TestCase):
    def test_fixed_matches_float_within_quantization_budget(self):
        config = load_config(os.path.join(ROOT, "params", "rank_config.json"))
        groups = load_groups(os.path.join(ROOT, "params", "feature_groups.json"))
        group_indices(groups, config["feature_names"])
        recall = [n for n in config["feature_names"] if n.startswith("recall_")]
        dense = [n for n in config["feature_names"] if n.startswith("dense_")]
        sparse = [n for n in config["feature_names"] if n.startswith("sparse_")]
        requests, _, _ = load_candidates(
            os.path.join(ROOT, "params", "candidates.jsonl"),
            len(dense), recall, sparse,
        )
        spec = FixedSpec(config["frac_bits"], config["levels"], config["rounding"])
        max_err = 0.0
        for raw in requests[:5]:
            req = prepare_candidates(raw, config["feature_names"], recall, dense, sparse)
            norm = normalize_request(req, config["feature_names"],
                                     config["normalization"], spec)
            fused = fuse_request(norm, config["weights"], config["temperature"],
                                 config["score_clip"], spec)
            ref = float_reference(
                req, config["feature_names"], config["normalization"],
                config["weights"], config["temperature"], config["score_clip"],
            )
            one = 1 << config["frac_bits"]
            for item, expected in zip(fused, ref):
                got = item["score"] / one
                err = abs(got - expected)
                max_err = max(max_err, err)
        # 粗量化（3 位 + 9 档）允许较宽预算，但必须显著小于未量化误差
        self.assertLess(max_err, 0.75)

    def test_higher_bits_tighten_error(self):
        config = load_config(os.path.join(ROOT, "params", "rank_config.json"))
        recall = [n for n in config["feature_names"] if n.startswith("recall_")]
        dense = [n for n in config["feature_names"] if n.startswith("dense_")]
        sparse = [n for n in config["feature_names"] if n.startswith("sparse_")]
        requests, _, _ = load_candidates(
            os.path.join(ROOT, "params", "candidates.jsonl"),
            len(dense), recall, sparse,
        )

        def worst_error(frac_bits, levels):
            spec = FixedSpec(frac_bits, levels, config["rounding"])
            req = prepare_candidates(requests[0], config["feature_names"],
                                     recall, dense, sparse)
            norm = normalize_request(req, config["feature_names"],
                                     config["normalization"], spec)
            fused = fuse_request(norm, config["weights"], config["temperature"],
                                 config["score_clip"], spec)
            ref = float_reference(req, config["feature_names"],
                                  config["normalization"], config["weights"],
                                  config["temperature"], config["score_clip"])
            one = 1 << frac_bits
            return max(abs(f["score"] / one - r) for f, r in zip(fused, ref))

        coarse = worst_error(2, 5)
        fine = worst_error(8, None)
        self.assertLess(fine, coarse)


if __name__ == "__main__":
    unittest.main()
