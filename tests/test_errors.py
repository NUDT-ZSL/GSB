from __future__ import annotations

import unittest

from poscascade.diagnostics import collect_error_scenarios
from poscascade.errors import ErrorCode

from .helpers import DATA


class ErrorChainTests(unittest.TestCase):
    def test_all_required_business_codes_are_attributed(self) -> None:
        report = collect_error_scenarios(DATA)
        self.assertTrue(report["ok"])
        observed = {item["actual_code"] for item in report["scenarios"]}
        required = {
            ErrorCode.E_DUP_DOC,
            ErrorCode.E_LINE_NO,
            ErrorCode.E_TEXT_EMPTY,
            ErrorCode.E_DICT_FORMAT,
            ErrorCode.E_LABEL_INVALID,
            ErrorCode.E_OFFSET_LEN,
            ErrorCode.E_UNATTRIBUTED,
        }
        self.assertTrue(required.issubset(observed), observed)
        by_name = {item["scenario"]: item for item in report["scenarios"]}
        self.assertEqual(by_name["duplicate_doc_id"]["doc_id"], "D9001")
        self.assertEqual(by_name["duplicate_doc_id"]["line_no"], 2)
        self.assertEqual(by_name["line_no_gap"]["doc_id"], "D9102")
        self.assertEqual(by_name["line_no_gap"]["line_no"], 3)
        self.assertEqual(by_name["empty_text"]["doc_id"], "D9201")
        self.assertEqual(by_name["offset_length_mismatch"]["doc_id"], "D9401")
        self.assertEqual(by_name["unattributed_fault"]["actual_code"], ErrorCode.E_UNATTRIBUTED)
