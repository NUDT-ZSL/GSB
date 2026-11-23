import unittest

from sampling_bench.errors import (
    E_DUP_REQUEST,
    E_LENGTH_INVALID,
    E_SAMPLING_INVALID,
    E_SEED_INVALID,
    E_VOCAB_VERSION_MISSING,
)
from sampling_bench.validation import validate_requests
from sampling_bench.dataset import load_vocab
from sampling_bench.errors import BenchError
from pathlib import Path
import json
import tempfile


def _raw(request_id="req-1", **overrides):
    base = {
        "request_id": request_id,
        "prompt_tokens": [1, 2, 3],
        "sampling_params": {"temperature": 1.0, "top_k": 0, "top_p": 1.0},
        "target_length": 5,
        "seed": 123,
        "priority": 0,
    }
    base.update(overrides)
    return base


class ValidationTest(unittest.TestCase):
    def test_duplicate_request(self):
        report = validate_requests([_raw(), _raw()])
        self.assertEqual(report.errors[0]["code"], E_DUP_REQUEST)
        self.assertEqual(len(report.valid_requests), 1)

    def test_invalid_sampling_params(self):
        report = validate_requests(
            [_raw("a", sampling_params={"temperature": 0, "top_k": 0, "top_p": 1.0})]
        )
        self.assertEqual(report.errors[0]["code"], E_SAMPLING_INVALID)
        self.assertEqual(report.errors[0]["field"], "sampling_params.temperature")

    def test_invalid_top_p(self):
        report = validate_requests(
            [_raw("a", sampling_params={"temperature": 1.0, "top_k": 0, "top_p": 1.5})]
        )
        self.assertEqual(report.errors[0]["code"], E_SAMPLING_INVALID)

    def test_invalid_length(self):
        report = validate_requests([_raw("a", target_length=0)])
        self.assertEqual(report.errors[0]["code"], E_LENGTH_INVALID)

    def test_unparseable_seed(self):
        report = validate_requests([_raw("a", seed="not-a-seed")])
        self.assertEqual(report.errors[0]["code"], E_SEED_INVALID)

    def test_one_bad_request_does_not_block_others(self):
        report = validate_requests([_raw("good"), _raw("bad", target_length=-2)])
        self.assertEqual([r.request_id for r in report.valid_requests], ["good"])
        self.assertEqual(report.errors[0]["request_id"], "bad")

    def test_vocab_version_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "vocab.json"
            path.write_text(
                json.dumps(
                    {"eos_token_id": 0, "id_to_token": ["<eos>", "a"]}
                ),
                encoding="utf-8",
            )
            with self.assertRaises(BenchError) as ctx:
                load_vocab(tmp)
            self.assertEqual(ctx.exception.code, E_VOCAB_VERSION_MISSING)


if __name__ == "__main__":
    unittest.main()
