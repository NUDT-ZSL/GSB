"""检测器报告与两版对比测试。"""

from __future__ import annotations

import unittest

from authsvc.constants import DEFAULT_SHARED_TOKEN
from detector.__main__ import _compare_reports
from detector.core import detect
from tests.support import start_in_process


class LegacyDetectionTests(unittest.TestCase):
    def test_legacy_report_marks_unauthorized_leaks_as_vulnerabilities(self) -> None:
        service = start_in_process("legacy")
        try:
            report = detect(service.base_url, DEFAULT_SHARED_TOKEN, label="legacy")
        finally:
            service.stop()

        payload = report.to_dict()
        checks = {check["id"]: check for check in payload["checks"]}

        self.assertFalse(payload["overall"]["clean"])
        self.assertEqual("vulnerability_found", payload["overall"]["status"])

        for check_id in (
            "unauthorized.missing_token",
            "unauthorized.empty_token",
            "unauthorized.wrong_token",
        ):
            with self.subTest(check=check_id):
                self.assertEqual("vulnerability", checks[check_id]["status"])
                self.assertEqual("not_rejected", checks[check_id]["outcome"])
                self.assertTrue(checks[check_id]["business_data_exposed"])

        # 旧版非法 JSON 泄漏内部异常细节，同样判为漏洞。
        self.assertEqual("vulnerability", checks["malformed.invalid_json"]["status"])
        self.assertTrue(checks["malformed.invalid_json"]["internal_detail_exposed"])

        # 合法请求与稳定性项仍应通过。
        self.assertEqual("pass", checks["legitimate.valid_token"]["status"])
        self.assertEqual("pass", checks["consistency.legitimate_data_stable"]["status"])

        summary = payload["summary"]
        self.assertEqual(3, summary["unauthorized_not_rejected"])
        self.assertEqual(0, summary["unauthorized_rejected"])

    def test_unauthorized_outcomes_are_partitioned(self) -> None:
        service = start_in_process("legacy")
        try:
            report = detect(service.base_url, DEFAULT_SHARED_TOKEN)
        finally:
            service.stop()
        unauthorized = [c for c in report.to_dict()["checks"] if c["category"] == "unauthorized"]
        self.assertEqual(3, len(unauthorized))
        self.assertTrue(all(c["outcome"] in {"rejected", "not_rejected"} for c in unauthorized))
        self.assertEqual(3, sum(1 for c in unauthorized if c["outcome"] == "not_rejected"))
        # 未拒绝项必须是漏洞，绝不可计为通过。
        for check in unauthorized:
            if check["outcome"] == "not_rejected":
                self.assertEqual("vulnerability", check["status"])
                self.assertTrue(check["is_vulnerability"])


class FixedDetectionTests(unittest.TestCase):
    def test_fixed_report_has_no_vulnerability_and_all_checks_pass(self) -> None:
        service = start_in_process("fixed")
        try:
            report = detect(service.base_url, DEFAULT_SHARED_TOKEN, label="fixed")
        finally:
            service.stop()

        payload = report.to_dict()
        self.assertTrue(payload["overall"]["clean"])
        self.assertEqual("no_vulnerability", payload["overall"]["status"])
        self.assertEqual(len(payload["checks"]), payload["summary"]["passed"])
        self.assertEqual(0, payload["summary"]["vulnerabilities"])
        self.assertEqual(0, payload["summary"]["failed"])
        self.assertEqual(3, payload["summary"]["unauthorized_rejected"])
        self.assertEqual(0, payload["summary"]["unauthorized_not_rejected"])
        for check in payload["checks"]:
            self.assertEqual("pass", check["status"], check["id"])

    def test_fixed_report_is_deterministic_across_runs(self) -> None:
        service = start_in_process("fixed")
        try:
            first = detect(service.base_url, DEFAULT_SHARED_TOKEN).to_dict()
            second = detect(service.base_url, DEFAULT_SHARED_TOKEN).to_dict()
        finally:
            service.stop()
        # base_url/目标端口在两版重启间会变；此处同一服务内两次结果应一致。
        for key in ("checks", "summary", "overall"):
            self.assertEqual(first[key], second[key])


class ComparisonTests(unittest.TestCase):
    def test_legacy_and_fixed_are_detected_independently_and_content_is_stable(self) -> None:
        legacy_service = start_in_process("legacy")
        fixed_service = start_in_process("fixed")

        # 先在旧版服务上取一份合法数据，再跑检测，最后确认数据一致。
        from detector.core import _post_json

        def legit(base_url: str) -> dict:
            response = _post_json(
                base_url, {"token": DEFAULT_SHARED_TOKEN, "account_id": "ACC-1001"}
            )
            return response.json()

        try:
            legacy_before = legit(legacy_service.base_url)
            fixed_before = legit(fixed_service.base_url)

            legacy_report = detect(legacy_service.base_url, DEFAULT_SHARED_TOKEN)
            fixed_report = detect(fixed_service.base_url, DEFAULT_SHARED_TOKEN)

            # 再次检测，验证结论可独立复现且互不影响。
            legacy_report_2 = detect(legacy_service.base_url, DEFAULT_SHARED_TOKEN)
            fixed_report_2 = detect(fixed_service.base_url, DEFAULT_SHARED_TOKEN)

            legacy_after = legit(legacy_service.base_url)
            fixed_after = legit(fixed_service.base_url)
        finally:
            legacy_service.stop()
            fixed_service.stop()

        self.assertEqual(legacy_before, legacy_after)
        self.assertEqual(fixed_before, fixed_after)
        self.assertEqual(
            legacy_report.to_dict()["checks"], legacy_report_2.to_dict()["checks"]
        )
        self.assertEqual(
            fixed_report.to_dict()["checks"], fixed_report_2.to_dict()["checks"]
        )

        comparison = _compare_reports(legacy_report, fixed_report)
        self.assertFalse(comparison["legacy"]["overall"]["clean"])
        self.assertTrue(comparison["fixed"]["overall"]["clean"])
        self.assertEqual(
            ["unauthorized.missing_token", "unauthorized.empty_token",
             "unauthorized.wrong_token", "malformed.invalid_json"],
            comparison["comparison"]["legacy_vulnerabilities"],
        )
        self.assertEqual([], comparison["comparison"]["fixed_vulnerabilities"])
        rows = {row["id"]: row for row in comparison["comparison"]["checks"]}
        for check_id in comparison["comparison"]["legacy_vulnerabilities"]:
            self.assertTrue(rows[check_id]["resolved"], check_id)
            self.assertEqual("pass", rows[check_id]["fixed_status"])


class ConstantTimeComparisonTests(unittest.TestCase):
    def test_security_module_uses_compare_digest(self) -> None:
        import inspect

        from authsvc import security

        source = inspect.getsource(security)
        self.assertIn("hmac.compare_digest", source)

    def test_fixed_server_has_no_plain_token_equality(self) -> None:
        import inspect
        import re

        from authsvc import fixed_server

        source = inspect.getsource(fixed_server)
        # 修复版模块中不得出现 supplied_token / shared_token 的普通 == / != 比较。
        plain_comparison = re.search(
            r"(?:supplied_token|shared_token|supplied_text|token_ok)[^\n]*(?:==|!=)", source
        )
        self.assertIsNone(
            plain_comparison,
            f"plain token comparison found: {plain_comparison.group(0) if plain_comparison else ''}",
        )
        self.assertIn("constant_time_token_match", source)

    def test_compare_digest_function_accepts_equal_and_unequal_tokens(self) -> None:
        from authsvc.security import constant_time_token_match

        self.assertTrue(constant_time_token_match("abc", "abc"))
        self.assertFalse(constant_time_token_match("abc", "abd"))
        self.assertFalse(constant_time_token_match("", "abc"))
        self.assertFalse(constant_time_token_match("a" * 1000, "abc"))


if __name__ == "__main__":
    unittest.main()
