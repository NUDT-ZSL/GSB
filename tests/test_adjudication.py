from __future__ import annotations

import unittest

from sampling_compat.errors import BusinessError, ErrorCode
from tests.helpers import temporary_app


class AdjudicationTests(unittest.TestCase):
    def test_equivalent_views_produce_same_tokens(self) -> None:
        temporary, app = temporary_app()
        self.addCleanup(temporary.cleanup)
        for request_id in ("req-0001", "req-0003"):
            conclusion = app.adjudicate(request_id)
            self.assertEqual(conclusion["status"], "CONSISTENT")
            self.assertEqual(conclusion["versions"], ["v1", "v2", "v3"])
            self.assertEqual(conclusion["step_count"], len(conclusion["token_ids"]))

    def test_v1_expressiveness_gap_is_explicit(self) -> None:
        temporary, app = temporary_app()
        self.addCleanup(temporary.cleanup)
        with self.assertRaises(BusinessError) as raised:
            app.adjudicate("req-0002")
        error = raised.exception
        self.assertEqual(error.code, ErrorCode.SEMANTIC_GAP)
        self.assertEqual(error.request_id, "req-0002")
        self.assertEqual(
            error.details["unrestorable_fields"],
            ["top_p", "repetition_penalty"],
        )
        self.assertEqual(error.details["affected_steps"], list(range(7)))

    def test_v2_v3_remain_consistent_for_rich_profile(self) -> None:
        temporary, app = temporary_app()
        self.addCleanup(temporary.cleanup)
        conclusion = app.adjudicate("req-0002", versions=("v2", "v3"))
        self.assertEqual(conclusion["status"], "CONSISTENT")
        self.assertEqual(conclusion["versions"], ["v2", "v3"])


if __name__ == "__main__":
    unittest.main()
