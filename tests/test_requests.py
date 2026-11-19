from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from sampling_compat.errors import BusinessError, ErrorCode
from sampling_compat.requests import load_requests_jsonl
from tests.helpers import make_app


class RequestLoadingTests(unittest.TestCase):
    def test_single_profile_and_default_seed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "requests.jsonl"
            path.write_text(
                json.dumps(
                    {
                        "request_id": "req-single",
                        "prompt_tokens": [1, 2],
                        "tokenizer_version": "tok-2024-01",
                        "sampling_profile": '{"version":"v1","temperature":"0.7","top_k":8}',
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            requests = load_requests_jsonl(path)
        request = requests["req-single"]
        self.assertEqual(request.profile_versions(), ("v1",))
        self.assertEqual(request.sampling_seed, 0)
        self.assertEqual(request.seed_source, "default_zero")

    def test_profile_view_header_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "requests.jsonl"
            path.write_text(
                json.dumps(
                    {
                        "request_id": "req-mismatch",
                        "prompt_tokens": [1, 2],
                        "tokenizer_version": "tok-2024-01",
                        "sampling_profile": {
                            "v2": '{"version":"v1","temperature":"0.7","top_k":8}'
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            with self.assertRaises(BusinessError) as raised:
                load_requests_jsonl(path)
        self.assertEqual(raised.exception.code, ErrorCode.FORMAT_CORRUPT)
        self.assertEqual(raised.exception.field, "sampling_profile.v2.version")

    def test_optional_logits_produces_no_logits_decision(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = make_app(root)
            path = root / "requests.jsonl"
            path.write_text(
                json.dumps(
                    {
                        "request_id": "req-no-logits",
                        "prompt_tokens": [1, 2],
                        "tokenizer_version": "tok-2024-01",
                        "sampling_profile": '{"version":"v1","temperature":"0.7","top_k":8}',
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            request = load_requests_jsonl(path)["req-no-logits"]
            decision = app._run_request(request, "v1", seed=0)
        self.assertEqual(decision["sampling_status"], "NO_LOGITS")
        self.assertEqual(decision["token_ids"], [])
        self.assertEqual(decision["step_traces"], [])


if __name__ == "__main__":
    unittest.main()
