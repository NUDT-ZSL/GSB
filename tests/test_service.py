"""服务本身的 HTTP 行为测试：未授权拒绝、合法通过、畸形输入。"""

from __future__ import annotations

import json
import unittest

from authsvc.constants import (
    DEFAULT_SHARED_TOKEN,
    QUERY_PATH,
    VALID_ACCOUNT_ID,
    WRONG_TOKEN_FOR_PROBE,
)
from authsvc.data import BUSINESS_FIELDS
from detector.core import Response, _post_json, _request, business_data_exposed
from tests.support import InProcessService, start_in_process

TOKEN = DEFAULT_SHARED_TOKEN


def _body(payload: dict) -> dict:
    return json.loads(json.dumps(payload))


def _business_keys_present(response: Response) -> bool:
    payload = response.json()
    if not isinstance(payload, dict):
        return False
    return any(field in payload for field in BUSINESS_FIELDS)


class LegitimateAccessTests(unittest.TestCase):
    def _assert_legitimate(self, service: InProcessService) -> None:
        response = _post_json(
            service.base_url, {"token": TOKEN, "account_id": VALID_ACCOUNT_ID}
        )
        self.assertEqual(200, response.status, response.text)
        payload = response.json()
        self.assertIsInstance(payload, dict)
        for field in BUSINESS_FIELDS:
            self.assertIn(field, payload, f"{service.version}: missing {field}")
        self.assertEqual("ACC-1001", payload["account_id"])
        self.assertEqual("184320.50", payload["balance"])
        self.assertTrue(payload["ledger"])
        self.assertTrue(business_data_exposed(response))

    def test_legacy_legitimate_query(self) -> None:
        service = start_in_process("legacy")
        try:
            self._assert_legitimate(service)
        finally:
            service.stop()

    def test_fixed_legitimate_query(self) -> None:
        service = start_in_process("fixed")
        try:
            self._assert_legitimate(service)
        finally:
            service.stop()


class UnauthorizedRejectionTests(unittest.TestCase):
    UNAUTHORIZED_PAYLOADS = {
        "missing": {"account_id": VALID_ACCOUNT_ID},
        "empty": {"token": "", "account_id": VALID_ACCOUNT_ID},
        "wrong": {"token": WRONG_TOKEN_FOR_PROBE, "account_id": VALID_ACCOUNT_ID},
    }

    def test_fixed_rejects_all_unauthorized_and_leaks_nothing(self) -> None:
        service = start_in_process("fixed")
        try:
            for name, payload in self.UNAUTHORIZED_PAYLOADS.items():
                with self.subTest(case=name):
                    response = _post_json(service.base_url, payload)
                    self.assertEqual(401, response.status, response.text)
                    self.assertFalse(
                        business_data_exposed(response),
                        f"{name}: business data leaked: {response.text}",
                    )
                    self.assertFalse(_business_keys_present(response))
                    self.assertNotIn("ACC-1001", response.text)
                    self.assertNotIn("184320.50", response.text)
                    self.assertNotIn("Traceback", response.text)
                    body = response.json()
                    self.assertEqual("unauthorized", body.get("error"))
        finally:
            service.stop()

    def test_legacy_leaks_for_all_unauthorized_cases(self) -> None:
        # 记录旧版缺陷，作为修复版对照基线。
        service = start_in_process("legacy")
        try:
            for name, payload in self.UNAUTHORIZED_PAYLOADS.items():
                with self.subTest(case=name):
                    response = _post_json(service.base_url, payload)
                    self.assertEqual(200, response.status, response.text)
                    self.assertTrue(business_data_exposed(response))
        finally:
            service.stop()

    def test_fixed_rejects_whitespace_only_and_wrong_case_token(self) -> None:
        service = start_in_process("fixed")
        try:
            for token in ("   ", TOKEN.upper(), TOKEN + " ", TOKEN[:-1]):
                with self.subTest(token=repr(token)):
                    response = _post_json(
                        service.base_url,
                        {"token": token, "account_id": VALID_ACCOUNT_ID},
                    )
                    self.assertEqual(401, response.status, response.text)
                    self.assertFalse(business_data_exposed(response))
        finally:
            service.stop()


class MalformedInputTests(unittest.TestCase):
    INVALID_JSON = b'{"token":"' + TOKEN.encode() + b'","account_id":"ACC-1001"'

    OVERSIZED = json.dumps(
        {"token": TOKEN, "account_id": "A" * 8192}, separators=(",", ":")
    ).encode()

    def test_fixed_rejects_invalid_json_without_internal_details(self) -> None:
        service = start_in_process("fixed")
        try:
            response = _request(service.base_url, "POST", QUERY_PATH, self.INVALID_JSON)
            self.assertEqual(400, response.status, response.text)
            self.assertNotIn("Traceback", response.text)
            self.assertNotIn(".py", response.text)
            self.assertFalse(business_data_exposed(response))
            # 紧接着合法请求仍可正常取数。
            follow_up = _post_json(
                service.base_url, {"token": TOKEN, "account_id": VALID_ACCOUNT_ID}
            )
            self.assertEqual(200, follow_up.status, follow_up.text)
            self.assertEqual("184320.50", follow_up.json()["balance"])
        finally:
            service.stop()

    def test_fixed_rejects_oversized_field_then_serves_legitimate(self) -> None:
        service = start_in_process("fixed")
        try:
            response = _request(service.base_url, "POST", QUERY_PATH, self.OVERSIZED)
            self.assertEqual(413, response.status, response.text)
            self.assertFalse(business_data_exposed(response))
            follow_up = _post_json(
                service.base_url, {"token": TOKEN, "account_id": VALID_ACCOUNT_ID}
            )
            self.assertEqual(200, follow_up.status)
            self.assertIn("ledger", follow_up.json())
        finally:
            service.stop()

    def test_fixed_rejects_non_object_json_and_non_string_account(self) -> None:
        service = start_in_process("fixed")
        try:
            for payload in ([], "text", 42, None, {"token": TOKEN, "account_id": 7}):
                with self.subTest(payload=payload):
                    response = _post_json(service.base_url, payload)
                    self.assertEqual(400, response.status, response.text)
                    self.assertFalse(business_data_exposed(response))
        finally:
            service.stop()


if __name__ == "__main__":
    unittest.main()
