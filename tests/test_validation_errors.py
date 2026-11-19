from __future__ import annotations

import unittest

from sampling_compat.errors import BusinessError, ErrorCode
from sampling_compat.requests import load_requests_jsonl
from tests.helpers import temporary_app


class ValidationErrorTests(unittest.TestCase):
    def test_error_demo_covers_all_business_codes(self) -> None:
        temporary, app = temporary_app()
        self.addCleanup(temporary.cleanup)
        outcomes = app.run_error_demo()
        self.assertEqual(len(outcomes), 12)
        self.assertTrue(all(outcome["passed"] for outcome in outcomes))
        self.assertEqual(
            {outcome["actual_code"] for outcome in outcomes},
            {
                ErrorCode.FORMAT_CORRUPT,
                ErrorCode.VERSION_UNSUPPORTED,
                ErrorCode.PARAM_RANGE,
                ErrorCode.TOKEN_RANGE,
                ErrorCode.LOGITS_SHAPE,
                ErrorCode.VOCAB_VERSION_MISMATCH,
                ErrorCode.SNAPSHOT_CORRUPT,
                ErrorCode.SEMANTIC_GAP,
            },
        )

    def test_token_range_attribution(self) -> None:
        temporary, app = temporary_app()
        self.addCleanup(temporary.cleanup)
        request = next(
            iter(
                load_requests_jsonl(
                    app.samples_dir
                    / "errors"
                    / "requests"
                    / "out_of_vocab_token.jsonl"
                ).values()
            )
        )
        with self.assertRaises(BusinessError) as raised:
            app._run_request(request, "v2", seed=request.sampling_seed)
        self.assertEqual(raised.exception.code, ErrorCode.TOKEN_RANGE)
        self.assertEqual(raised.exception.request_id, "err-token-range")
        self.assertEqual(raised.exception.details["token_index"], 1)

    def test_logits_shape_attribution_names_step(self) -> None:
        temporary, app = temporary_app()
        self.addCleanup(temporary.cleanup)
        request = next(
            iter(
                load_requests_jsonl(
                    app.samples_dir
                    / "errors"
                    / "requests"
                    / "logits_shape_mismatch.jsonl"
                ).values()
            )
        )
        with self.assertRaises(BusinessError) as raised:
            app._run_request(request, "v2", seed=request.sampling_seed)
        self.assertEqual(raised.exception.code, ErrorCode.LOGITS_SHAPE)
        self.assertEqual(raised.exception.request_id, "err-logits-shape")
        self.assertEqual(raised.exception.step, 2)

    def test_vocab_binding_mismatch_attribution(self) -> None:
        temporary, app = temporary_app()
        self.addCleanup(temporary.cleanup)
        request = next(
            iter(
                load_requests_jsonl(
                    app.samples_dir / "errors" / "requests" / "vocab_mismatch.jsonl"
                ).values()
            )
        )
        with self.assertRaises(BusinessError) as raised:
            app._run_request(request, "v2", seed=request.sampling_seed)
        self.assertEqual(raised.exception.code, ErrorCode.VOCAB_VERSION_MISMATCH)
        self.assertEqual(raised.exception.request_id, "err-vocab-mismatch")
        self.assertEqual(raised.exception.field, "vocab_version")


if __name__ == "__main__":
    unittest.main()
