import unittest

from prank.errors import BUSINESS_CODES, BusinessError, exit_code_for


class ErrorChainTests(unittest.TestCase):
    def test_exit_codes_portable(self):
        for code, offset in BUSINESS_CODES.items():
            exit_code = exit_code_for(code)
            self.assertEqual(exit_code, 20 + offset)
            self.assertLessEqual(exit_code, 255)

    def test_chain_carries_upstream_codes(self):
        root = BusinessError("E_VALUE_RANGE", "validate", "bad price",
                             query_id="q1", field="price")
        downstream = BusinessError("E_INCONSISTENT", "rerank",
                                   "fallback rerank aborted", query_id="q1")
        chained = BusinessError.chain(downstream, root)
        payload = chained.to_dict()
        self.assertEqual(payload["code"], "E_INCONSISTENT")
        self.assertEqual(payload["upstream_chain"], ["E_VALUE_RANGE"])
        self.assertEqual(payload["upstream_detail"][0]["code"], "E_VALUE_RANGE")
        self.assertEqual(payload["query_id"], "q1")

    def test_nested_chain_order(self):
        first = BusinessError("E_ITEM_EMPTY", "validate", "empty", query_id="q")
        second = BusinessError.chain(
            BusinessError("E_SCALE_INVALID", "precision", "scale", query_id="q"),
            first)
        third = BusinessError.chain(
            BusinessError("E_INCONSISTENT", "rerank", "abort", query_id="q"),
            second)
        self.assertEqual(third.to_dict()["upstream_chain"],
                         ["E_ITEM_EMPTY", "E_SCALE_INVALID"])


if __name__ == "__main__":
    unittest.main()
