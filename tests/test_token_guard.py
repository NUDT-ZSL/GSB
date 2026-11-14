"""token_guard 验收测试：对应任务书 9 条验收点，全部使用确定性样本。

运行（项目根目录）：
    python -m unittest -v
或：
    python tests/test_token_guard.py
"""

from __future__ import annotations

import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

# 兼容“直接运行该文件”的场景：把项目根目录加入 sys.path。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from token_guard import (  # noqa: E402
    MAX_TOKEN_LENGTH,
    TokenTooLongError,
    UnsupportedTokenError,
    analyze_token,
    hardened_verify,
)
from token_guard.analyzer import _sort_risks  # noqa: E402
from token_guard.cli import main as cli_main  # noqa: E402
from token_guard.core import SEVERITY_ORDER  # noqa: E402
from token_guard.detector import TOKEN_TYPE_CUSTOM, TOKEN_TYPE_JWT, detect_token_type  # noqa: E402
from token_guard.samples import DEMO_KEY, build_samples, make_custom_token, make_jwt  # noqa: E402

SAMPLES = build_samples()


def risk_codes(report: dict) -> list[str]:
    return [r["code"] for r in report["risks"]]


class Test1TokenTypeDetection(unittest.TestCase):
    """验收点 1：区分 JWT 与自定义格式；空串/结构不符报不支持错误。"""

    def test_valid_jwt_recognized(self):
        self.assertEqual(detect_token_type(SAMPLES["jwt_valid"]), TOKEN_TYPE_JWT)

    def test_valid_custom_recognized(self):
        self.assertEqual(detect_token_type(SAMPLES["custom_valid"]), TOKEN_TYPE_CUSTOM)

    def test_stripped_two_segment_jwt_still_jwt(self):
        # 2 段但首段是 JWT 头 -> 仍识别为 JWT（签名剥离场景）
        self.assertEqual(detect_token_type(SAMPLES["jwt_stripped"]), TOKEN_TYPE_JWT)

    def test_empty_string_unsupported(self):
        with self.assertRaises(UnsupportedTokenError):
            detect_token_type(SAMPLES["empty_string"])

    def test_garbage_unsupported(self):
        with self.assertRaises(UnsupportedTokenError):
            detect_token_type(SAMPLES["garbage"])

    def test_four_segments_unsupported(self):
        with self.assertRaises(UnsupportedTokenError):
            detect_token_type(SAMPLES["four_segments"])


class Test2AlgorithmTampering(unittest.TestCase):
    """验收点 2：alg=none 必须出现在风险列表中。"""

    def test_alg_none_reported(self):
        report = analyze_token(SAMPLES["jwt_alg_none"], DEMO_KEY)
        self.assertIn("alg_none", risk_codes(report))
        alg_risk = next(r for r in report["risks"] if r["code"] == "alg_none")
        self.assertEqual(alg_risk["severity"], "critical")


class Test3SignatureStripping(unittest.TestCase):
    """验收点 3：无签名段 JWT 报告签名剥离风险。"""

    def test_stripped_jwt_reported(self):
        report = analyze_token(SAMPLES["jwt_stripped"], DEMO_KEY)
        self.assertIn("signature_stripped", risk_codes(report))

    def test_empty_signature_segment_reported(self):
        head, payload, _sig = SAMPLES["jwt_valid"].split(".")
        report = analyze_token(f"{head}.{payload}.", DEMO_KEY)
        self.assertIn("signature_stripped", risk_codes(report))

    def test_custom_stripped_reported(self):
        report = analyze_token(SAMPLES["custom_stripped"], DEMO_KEY)
        self.assertIn("signature_stripped", risk_codes(report))


class Test4PayloadTampering(unittest.TestCase):
    """验收点 4：改载荷留旧签名，报告载荷篡改风险。"""

    def test_jwt_payload_tamper_reported(self):
        report = analyze_token(SAMPLES["jwt_payload_tampered"], DEMO_KEY)
        self.assertIn("payload_tampered", risk_codes(report))

    def test_custom_payload_tamper_reported(self):
        report = analyze_token(SAMPLES["custom_payload_tampered"], DEMO_KEY)
        self.assertIn("signature_invalid", risk_codes(report))


class Test5HardenedJwtVerification(unittest.TestCase):
    """验收点 5：合法 JWT 通过；三类篡改令牌全部拒绝。"""

    def test_valid_jwt_passes(self):
        result = hardened_verify(SAMPLES["jwt_valid"], DEMO_KEY)
        self.assertTrue(result["passed"], result)

    def test_alg_none_rejected(self):
        result = hardened_verify(SAMPLES["jwt_alg_none"], DEMO_KEY)
        self.assertFalse(result["passed"])
        self.assertEqual(result["reason"], "alg_tampered")

    def test_stripped_rejected(self):
        result = hardened_verify(SAMPLES["jwt_stripped"], DEMO_KEY)
        self.assertFalse(result["passed"])
        self.assertEqual(result["reason"], "signature_stripped")

    def test_payload_tamper_rejected(self):
        result = hardened_verify(SAMPLES["jwt_payload_tampered"], DEMO_KEY)
        self.assertFalse(result["passed"])
        self.assertEqual(result["reason"], "payload_tampered")

    def test_wrong_key_rejected(self):
        result = hardened_verify(SAMPLES["jwt_valid"], "another-wrong-key")
        self.assertFalse(result["passed"])
        self.assertEqual(result["reason"], "payload_tampered")


class Test6HardenedCustomVerification(unittest.TestCase):
    """验收点 6：自定义 payload.signature 用 HMAC-SHA256 校验。"""

    def test_valid_custom_passes(self):
        result = hardened_verify(SAMPLES["custom_valid"], DEMO_KEY)
        self.assertTrue(result["passed"], result)

    def test_custom_payload_tamper_rejected(self):
        result = hardened_verify(SAMPLES["custom_payload_tampered"], DEMO_KEY)
        self.assertFalse(result["passed"])
        self.assertEqual(result["reason"], "signature_invalid")

    def test_custom_signature_tamper_rejected(self):
        result = hardened_verify(SAMPLES["custom_signature_tampered"], DEMO_KEY)
        self.assertFalse(result["passed"])
        self.assertEqual(result["reason"], "signature_invalid")

    def test_custom_with_bytes_key(self):
        result = hardened_verify(SAMPLES["custom_valid"], DEMO_KEY.encode("utf-8"))
        self.assertTrue(result["passed"])

    def test_roundtrip_constructors(self):
        # 构造器产出的两种令牌都应被各自流程接受
        self.assertTrue(hardened_verify(make_jwt({"sub": "x"}), DEMO_KEY)["passed"])
        self.assertTrue(hardened_verify(make_custom_token({"sub": "x"}), DEMO_KEY)["passed"])


class Test7JsonReportAndOrdering(unittest.TestCase):
    """验收点 7：JSON 字段齐全，风险按严重程度从高到低排列。"""

    REQUIRED_FIELDS = {
        "token_type", "token_length", "algorithm", "risk_count",
        "risks", "verification", "conclusion",
    }

    def test_report_fields_complete(self):
        report = analyze_token(SAMPLES["jwt_valid"], DEMO_KEY)
        self.assertTrue(self.REQUIRED_FIELDS.issubset(report.keys()))
        self.assertEqual(report["risk_count"], 0)
        self.assertEqual(report["conclusion"], "pass")
        self.assertTrue(report["verification"]["passed"])
        # 必须是可 JSON 序列化的
        json.dumps(report, ensure_ascii=False)

    def test_risk_entry_fields_complete(self):
        report = analyze_token(SAMPLES["jwt_alg_none"], DEMO_KEY)
        for risk in report["risks"]:
            self.assertEqual(
                set(risk.keys()),
                {"code", "severity", "title", "detail", "detected_at"},
            )

    def test_risks_sorted_by_severity_desc(self):
        report = analyze_token(SAMPLES["jwt_combo_attack"], DEMO_KEY)
        severities = [SEVERITY_ORDER[r["severity"]] for r in report["risks"]]
        self.assertEqual(severities, sorted(severities, reverse=True))
        # 复合攻击至少含：alg none（critical）、签名剥离（critical）、过期（medium）
        codes = risk_codes(report)
        self.assertIn("alg_none", codes)
        self.assertIn("signature_stripped", codes)
        self.assertIn("token_expired", codes)

    def test_sort_is_stable_for_equal_severity(self):
        risks = [
            {"severity": "medium", "code": "a"},
            {"severity": "critical", "code": "b"},
            {"severity": "medium", "code": "c"},
        ]
        ordered = _sort_risks(risks)
        self.assertEqual([r["code"] for r in ordered], ["b", "a", "c"])

    def test_combo_conclusion_is_fail(self):
        report = analyze_token(SAMPLES["jwt_combo_attack"], DEMO_KEY)
        self.assertEqual(report["conclusion"], "fail")


class Test8Expiration(unittest.TestCase):
    """验收点 8：exp 过期 -> 校验拒绝且报告含过期风险项。"""

    def test_expired_jwt_rejected_and_flagged(self):
        report = analyze_token(SAMPLES["jwt_expired"], DEMO_KEY)
        self.assertIn("token_expired", risk_codes(report))
        self.assertEqual(report["conclusion"], "fail")
        result = hardened_verify(SAMPLES["jwt_expired"], DEMO_KEY)
        self.assertFalse(result["passed"])
        self.assertEqual(result["reason"], "token_expired")

    def test_expired_custom_rejected(self):
        from token_guard.samples import NOW
        token = make_custom_token({"sub": "u", "exp": NOW - 10})
        result = hardened_verify(token, DEMO_KEY)
        self.assertFalse(result["passed"])
        self.assertEqual(result["reason"], "token_expired")


class Test9LengthLimit(unittest.TestCase):
    """验收点 9：超过 1000 字符给明确错误而非崩溃。"""

    def test_too_long_raises_typed_error(self):
        with self.assertRaises(TokenTooLongError) as ctx:
            analyze_token(SAMPLES["too_long"], DEMO_KEY)
        self.assertIn(str(MAX_TOKEN_LENGTH), str(ctx.exception))

    def test_too_long_verify_same_guard(self):
        with self.assertRaises(TokenTooLongError):
            hardened_verify("x" * (MAX_TOKEN_LENGTH + 1), DEMO_KEY)

    def test_boundary_length_accepted_by_guard(self):
        # 恰好 1000 字符不应触发长度错误（结构按 2 段自定义格式识别）
        token = "a." + ("x" * (MAX_TOKEN_LENGTH - 2))
        self.assertEqual(len(token), MAX_TOKEN_LENGTH)
        self.assertEqual(detect_token_type(token), TOKEN_TYPE_CUSTOM)
        # 1001 字符立即触发超长错误
        with self.assertRaises(TokenTooLongError):
            detect_token_type(token + "x")

    def test_cli_too_long_exit_code_2(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli_main(["analyze", "--token", SAMPLES["too_long"], "--key", DEMO_KEY])
        self.assertEqual(code, 2)
        payload = json.loads(buf.getvalue())
        self.assertTrue(payload["error"])
        self.assertEqual(payload["code"], "token_too_long")


class TestCliBehavior(unittest.TestCase):
    """CLI 端到端行为补充。"""

    def test_analyze_valid_exit_0(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli_main(["analyze", "--token", SAMPLES["jwt_valid"], "--key", DEMO_KEY])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(buf.getvalue())["conclusion"], "pass")

    def test_verify_tampered_exit_1(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli_main(["verify", "--token", SAMPLES["jwt_alg_none"], "--key", DEMO_KEY])
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(buf.getvalue())["passed"])

    def test_unsupported_exit_2(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli_main(["analyze", "--token", SAMPLES["garbage"], "--key", DEMO_KEY])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(buf.getvalue())["code"], "unsupported_token")


if __name__ == "__main__":
    unittest.main(verbosity=2)
