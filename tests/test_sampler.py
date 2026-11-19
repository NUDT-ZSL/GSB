from __future__ import annotations

import json
import unittest

from sampling_compat.config_io import read_config
from sampling_compat.normalization import normalize_params
from sampling_compat.reference import reference_sample
from sampling_compat.utils import canonical_json
from tests.helpers import SAMPLES, temporary_app


class SamplerTests(unittest.TestCase):
    def test_expected_tokens_match_committed_fixture(self) -> None:
        temporary, app = temporary_app()
        self.addCleanup(temporary.cleanup)
        expected = json.loads((SAMPLES / "expected.json").read_text(encoding="utf-8"))
        for request_id, request in app.requests.items():
            for version in request.profile_versions():
                decision = app.run_request(
                    request_id,
                    version,
                    seed=request.sampling_seed,
                )
                self.assertEqual(
                    decision["token_ids"],
                    expected["requests"][request_id][version]["token_ids"],
                )
                self.assertEqual(
                    decision["token_sha256"],
                    expected["requests"][request_id][version]["token_sha256"],
                )

    def test_repeat_runs_are_byte_identical(self) -> None:
        temporary, app = temporary_app()
        self.addCleanup(temporary.cleanup)
        first = app.run_request("req-0002", "v2")
        first_bytes = canonical_json(first)
        second = app.run_request("req-0002", "v2")
        self.assertEqual(first_bytes, canonical_json(second))
        self.assertEqual(first["artifact_checksum"], second["artifact_checksum"])

    def test_trace_candidate_weights_are_normalized(self) -> None:
        temporary, app = temporary_app()
        self.addCleanup(temporary.cleanup)
        decision = app.run_request("req-0002", "v2")
        for trace in decision["step_traces"]:
            weights = [candidate["weight"] for candidate in trace["candidates"]]
            self.assertAlmostEqual(sum(weights), 1.0, places=14)
            self.assertEqual(weights[-1], trace["candidates"][-1]["weight"])
            self.assertLessEqual(len(trace["candidate_ids"]), trace["top_k_requested"])
            self.assertIn(trace["chosen_token"], trace["candidate_ids"])

    def test_independent_float64_reference_agrees(self) -> None:
        temporary, app = temporary_app()
        self.addCleanup(temporary.cleanup)
        for request_id, request in app.requests.items():
            for version in request.profile_versions():
                decision = app.run_request(
                    request_id,
                    version,
                    seed=request.sampling_seed,
                )
                vocab_entry = app._validate_binding(
                    request,
                    read_config(request.sampling_profiles[version]),
                )
                params = normalize_params(
                    read_config(request.sampling_profiles[version]),
                    vocabulary_size=vocab_entry.size,
                )
                logits = app._validate_logits(request, vocab_entry)
                reference_tokens, reference_traces = reference_sample(
                    request_id=request_id,
                    prompt_tokens=request.prompt_tokens,
                    logits=logits.tolist(),
                    params=params,
                    seed=request.sampling_seed,
                )
                self.assertEqual(reference_tokens, decision["token_ids"])
                for reference_trace, stored_trace in zip(
                    reference_traces,
                    decision["step_traces"],
                    strict=True,
                ):
                    stored_weights = [
                        item["weight"] for item in stored_trace["candidates"]
                    ]
                    for left, right in zip(
                        reference_trace["weights"],
                        stored_weights,
                        strict=True,
                    ):
                        self.assertAlmostEqual(left, right, places=14)

    def test_uniform_seed_changes_sampling_input_not_algorithm(self) -> None:
        temporary, app = temporary_app()
        self.addCleanup(temporary.cleanup)
        first = app.run_request("req-0001", "v2", seed=1)
        second = app.run_request("req-0001", "v2", seed=2)
        self.assertNotEqual(first["artifact_checksum"], second["artifact_checksum"])
        self.assertEqual(first["sampling_algorithm"], second["sampling_algorithm"])


if __name__ == "__main__":
    unittest.main()
