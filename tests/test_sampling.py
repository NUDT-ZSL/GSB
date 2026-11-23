import json
import unittest

import numpy as np

from llm_workbench.sampling import SamplingParams, sample_request, truncate_candidates


class TestDeterministicSampling(unittest.TestCase):
    def test_same_seed_byte_identical(self):
        params = SamplingParams(temperature=0.9, top_k=16, top_p=0.95)
        a = sample_request("r1", seed=1234, target_len=32, params=params, vocab_size=128)
        b = sample_request("r1", seed=1234, target_len=32, params=params, vocab_size=128)
        ca = json.dumps(a.to_dict(), sort_keys=True)
        cb = json.dumps(b.to_dict(), sort_keys=True)
        self.assertEqual(ca, cb)

    def test_different_seed_differs(self):
        params = SamplingParams()
        a = sample_request("r1", seed=1, target_len=16, params=params, vocab_size=64)
        b = sample_request("r1", seed=2, target_len=16, params=params, vocab_size=64)
        self.assertNotEqual(a.tokens, b.tokens)

    def test_tie_break_ascending_token_id(self):
        logits = np.zeros(8, dtype=np.float64)
        params = SamplingParams(temperature=1.0, top_k=3, top_p=1.0)
        cand_ids, cand_probs, cut = truncate_candidates(logits, params)
        self.assertEqual(list(cand_ids), [0, 1, 2])
        self.assertEqual(cut, 5)
        for p in cand_probs:
            self.assertAlmostEqual(p, 1.0 / 3.0)

    def test_top_k_keeps_highest(self):
        logits = np.arange(10, dtype=np.float64)
        params = SamplingParams(temperature=1.0, top_k=2, top_p=1.0)
        cand_ids, _, cut = truncate_candidates(logits, params)
        self.assertEqual(list(cand_ids), [9, 8])
        self.assertEqual(cut, 8)

    def test_top_p_minimal_prefix(self):
        logits = np.log(np.array([0.5, 0.25, 0.125, 0.125]))
        params = SamplingParams(temperature=1.0, top_k=None, top_p=0.7)
        cand_ids, cand_probs, _ = truncate_candidates(logits, params)
        self.assertEqual(list(cand_ids), [0, 1])
        self.assertAlmostEqual(float(np.sum(cand_probs)), 1.0)

    def test_top_p_keeps_at_least_one(self):
        logits = np.array([10.0, 0.0, 0.0])
        params = SamplingParams(temperature=1.0, top_k=None, top_p=0.01)
        cand_ids, _, _ = truncate_candidates(logits, params)
        self.assertEqual(list(cand_ids), [0])

    def test_no_global_rng_dependence(self):
        params = SamplingParams(temperature=1.1, top_k=8, top_p=0.9)
        a = sample_request("r", seed=7, target_len=8, params=params, vocab_size=32)
        np.random.seed(99999)
        np.random.rand(100)
        b = sample_request("r", seed=7, target_len=8, params=params, vocab_size=32)
        self.assertEqual(a.to_dict(), b.to_dict())


if __name__ == "__main__":
    unittest.main()
