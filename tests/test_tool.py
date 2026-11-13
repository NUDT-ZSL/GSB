"""工具纯逻辑组件的单元测试（仅标准库 unittest）。

运行：
  python -I -X utf8 tests/test_tool.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from order_sec_check.config import CaseValidationError, load_case_file  # noqa: E402
from order_sec_check.netpolicy import PolicyError, assert_local_url  # noqa: E402
from order_sec_check.redact import (  # noqa: E402
    REDACTION_PLACEHOLDER,
    redact_headers,
    redact_text,
)
from order_sec_check.report import (  # noqa: E402
    sort_entries,
    unique_report_path,
    write_json,
)
from order_sec_check.validate import ParamValidationError, check_param_rules  # noqa: E402

GOOD_CASE = {
    "target": {"base_url": "http://127.0.0.1:18080"},
    "cases": [
        {
            "case_id": "TC-01",
            "type": "sqli",
            "description": "x",
            "request": {
                "method": "GET",
                "path": "/orders",
                "headers": {"X-Auth-Token": "tok-x"},
                "params": {"order_id": "1", "user_id": "alice"},
            },
            "rules": [{"type": "status_code_equals", "expected": 200}],
        }
    ],
}


class ConfigValidationTests(unittest.TestCase):
    def _load(self, payload) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cases.json"
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            load_case_file(path)

    def test_valid_file_passes(self):
        self._load(GOOD_CASE)

    def test_missing_required_field_reported_with_json_path(self):
        bad = json.loads(json.dumps(GOOD_CASE))
        del bad["cases"][0]["description"]
        bad["cases"][0]["request"]["method"] = 999
        with self.assertRaises(CaseValidationError) as ctx:
            self._load(bad)
        joined = " | ".join(ctx.exception.errors)
        self.assertIn("$.cases[0].description", joined)
        self.assertIn("$.cases[0].request.method", joined)

    def test_wrong_type_and_unknown_enum(self):
        bad = json.loads(json.dumps(GOOD_CASE))
        bad["cases"][0]["type"] = "xss"
        bad["cases"][0]["rules"][0]["type"] = "no_such_rule"
        with self.assertRaises(CaseValidationError) as ctx:
            self._load(bad)
        self.assertTrue(any("type" in e for e in ctx.exception.errors))

    def test_duplicate_case_id_rejected(self):
        bad = json.loads(json.dumps(GOOD_CASE))
        bad["cases"].append(json.loads(json.dumps(bad["cases"][0])))
        with self.assertRaises(CaseValidationError) as ctx:
            self._load(bad)
        self.assertTrue(any("重复" in e for e in ctx.exception.errors))

    def test_malformed_json_reports_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cases.json"
            path.write_text("{ not json", encoding="utf-8")
            with self.assertRaises(CaseValidationError) as ctx:
                load_case_file(path)
            self.assertIn("不是合法 JSON", ctx.exception.errors[0])


class NetPolicyTests(unittest.TestCase):
    def test_loopback_ip_allowed(self):
        assert_local_url("http://127.0.0.1:18080/orders")
        assert_local_url("http://localhost:18080")
        assert_local_url("http://[::1]:18080")

    def test_public_host_rejected(self):
        with self.assertRaises(PolicyError):
            assert_local_url("http://93.184.216.34:18080/orders")

    def test_external_domain_rejected(self):
        with self.assertRaises(PolicyError):
            assert_local_url("http://order-db.example.invalid/orders")

    def test_non_http_scheme_rejected(self):
        with self.assertRaises(PolicyError):
            assert_local_url("file:///etc/passwd")


class ParamValidationTests(unittest.TestCase):
    def test_compliant_params_pass(self):
        check_param_rules(
            {"order_id": "1001", "user_id": "alice"},
            {"order_id": ["positive_int"], "user_id": ["not_empty"]},
        )

    def test_empty_user_id_rejected_with_reason(self):
        with self.assertRaises(ParamValidationError) as ctx:
            check_param_rules(
                {"order_id": "1001", "user_id": "  "},
                {"user_id": ["not_empty"]},
            )
        self.assertTrue(any("为空" in r for r in ctx.exception.reasons))

    def test_non_numeric_order_id_rejected(self):
        with self.assertRaises(ParamValidationError) as ctx:
            check_param_rules(
                {"order_id": "abc;DROP"},
                {"order_id": ["positive_int"]},
            )
        self.assertTrue(any("正整数" in r for r in ctx.exception.reasons))

    def test_zero_and_negative_rejected(self):
        for value in ("0", "-3", "1.5"):
            with self.assertRaises(ParamValidationError):
                check_param_rules({"order_id": value}, {"order_id": ["positive_int"]})


class RedactionTests(unittest.TestCase):
    def test_sensitive_headers_redacted(self):
        safe = redact_headers({"X-Auth-Token": "tok-alice-001", "X-User-Id": "alice"})
        self.assertEqual(safe["X-Auth-Token"], REDACTION_PLACEHOLDER)
        self.assertEqual(safe["X-User-Id"], "alice")

    def test_text_secret_replaced(self):
        text = "url?token=tok-bob-002 end"
        self.assertNotIn("tok-bob-002", redact_text(text, ["tok-bob-002"]))

    def test_empty_input(self):
        self.assertEqual(redact_text("", ["x"]), "")


class ReportTests(unittest.TestCase):
    def test_sort_by_case_number(self):
        entries = [{"case_id": "TC-10"}, {"case_id": "TC-2"}, {"case_id": "TC-01"}]
        self.assertEqual(
            [e["case_id"] for e in sort_entries(entries)],
            ["TC-01", "TC-2", "TC-10"],
        )

    def test_existing_file_never_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            first = unique_report_path(directory, "detection_report")
            write_json(first, {"v": 1})
            second = unique_report_path(directory, "detection_report")
            self.assertNotEqual(first, second)
            write_json(second, {"v": 2})
            self.assertEqual(json.loads(first.read_text(encoding="utf-8"))["v"], 1)
            self.assertEqual(json.loads(second.read_text(encoding="utf-8"))["v"], 2)
            with self.assertRaises(FileExistsError):
                open(first, "x", encoding="utf-8").close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
