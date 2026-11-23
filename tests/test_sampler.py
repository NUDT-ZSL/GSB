import json
import unittest
from pathlib import Path

import numpy as np

from sampling_bench.dataset import generate_dataset, load_logits, load_requests, load_vocab
from sampling_bench.models import Request, SamplingParams
from sampling_bench.sampler import (
    apply_top_k,
    apply_top_p,
    inverse_cdf_sample,
    sample_request,
)


class SamplerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data_dir = Path(__file__).parent / "_data_sampler"
        generate_dataset(cls.data_dir, num_requests=8, seed=424242)
        cls.vocab = load_vocab(cls.data_dir)
        cls.requests = load_requests(cls.data_dir)

    def test_byte_identical_repeat(self):
        request = self.requests[0]
        logits = load_logits(self.data_dir, request.request_id)
        first = sample_request(request, self.vocab, logits)
        second = sample_request(request, self.vocab, logits)
        self.assertEqual(
            json.dumps(first.to_dict(), sort_keys=True),
            json.dumps(second.to_dict(), sort_keys=True),
        )

    def test_distinct_seed_distinct_stream(self):
        request = self.requests[1]
        logits = load_logits(self.data_dir, request.request_id)
        changed = Request(
            request_id=request.request_id,
            prompt_tokens=request.prompt_tokens,
            sampling_params=request.sampling_params,
            target_length=request.target_length,
            seed=request.seed + 1,
            priority=request.priority,
        )
        baseline = sample_request(request, self.vocab, logits)
        other = sample_request(changed, self.vocab, logits)
        uniforms_a = [step.random_uniform for step in baseline.steps]
        uniforms_b = [step.random_uniform for step in other.steps]
        self.assertNotEqual(uniforms_a, uniforms_b)

    def test_top_k_limits_candidates(self):
        logits = np.array([3.0, 1.0, 2.0, 0.0])
        ids = np.arange(4)
        kept = apply_top_k(ids, logits, 2)
        self.assertEqual(set(int(x) for x in kept), {0, 2})

    def test_top_p_minimal_prefix(self):
        ids = np.array([0, 1, 2])
        probs = np.array([0.6, 0.3, 0.1])
        kept = apply_top_p(ids, probs, 0.85)
        self.assertEqual(set(int(x) for x in kept), {0, 1})

    def test_inverse_cdf_deterministic(self):
        ids = np.array([0, 1, 2])
        probs = np.array([0.5, 0.4, 0.1])
        self.assertEqual(inverse_cdf_sample(ids, probs, 0.1), 0)
        self.assertEqual(inverse_cdf_sample(ids, probs, 0.55), 1)
        self.assertEqual(inverse_cdf_sample(ids, probs, 0.95), 2)

    def test_eos_completion(self):
        request = self.requests[2]
        logits = load_logits(self.data_dir, request.request_id).copy()
        logits[:, self.vocab.eos_token_id] = 1_000.0
        result = sample_request(request, self.vocab, logits)
        self.assertTrue(result.finished_by_eos)
        self.assertEqual(result.generated_tokens[0], self.vocab.eos_token_id)
        self.assertEqual(len(result.steps), 1)

    def test_top_p_override_changes_records(self):
        request = self.requests[3]
        logits = load_logits(self.data_dir, request.request_id)
        baseline = sample_request(request, self.vocab, logits)
        tight = SamplingParams(
            temperature=request.sampling_params.temperature,
            top_k=request.sampling_params.top_k,
            top_p=0.2,
        )
        capped = sample_request(request, self.vocab, logits, param_override=tight)
        for capped_step, base_step in zip(capped.steps, baseline.steps):
            self.assertLessEqual(
                len(capped_step.top_p_kept), len(base_step.top_p_kept)
            )


if __name__ == "__main__":
    unittest.main()
