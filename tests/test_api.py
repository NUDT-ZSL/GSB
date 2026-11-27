"""Flask HTTP 接口测试。"""

import json
import unittest

from app import create_app
from museum_anim.sample_data import (
    CAMERA_CONFIG,
    CASE_BOUNDS,
    SCRIPT_ENTRIES,
    SCRIPT_ENTRIES_WITH_INVALID,
    build_vase_obj,
)


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.client = self.app.test_client()

    def test_health(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["status"], "ok")

    def _payload(self, script=None):
        return {
            "fps": 25,
            "obj_text": build_vase_obj(),
            "script": script or SCRIPT_ENTRIES,
            "case_bounds": CASE_BOUNDS,
            "camera": CAMERA_CONFIG,
        }

    def test_animate_success(self):
        response = self.client.post("/api/animate", json=self._payload())
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["status"], "OK")
        self.assertIn("clips", data)
        self.assertIn("summary_report", data)
        self.assertIn("verification_report", data)

    def test_animate_unparsed_error(self):
        response = self.client.post(
            "/api/animate", json=self._payload(SCRIPT_ENTRIES_WITH_INVALID)
        )
        self.assertEqual(response.status_code, 422)
        data = response.get_json()
        self.assertEqual(data["code"], "E_UNPARSED")
        self.assertEqual(data["entry_index"], 5)

    def test_animate_bad_input(self):
        response = self.client.post("/api/animate", json={"fps": 25})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["code"], "E_INPUT")

    def test_parse_endpoint(self):
        response = self.client.post("/api/parse", json={"script": SCRIPT_ENTRIES})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.get_json()["parsed_actions"]), len(SCRIPT_ENTRIES))

    def test_stateless_independent_requests(self):
        payload = self._payload()
        first = self.client.post("/api/animate", json=payload).get_json()
        second = self.client.post("/api/animate", json=payload).get_json()
        self.assertEqual(
            json.dumps(first, sort_keys=True),
            json.dumps(second, sort_keys=True),
        )


if __name__ == "__main__":
    unittest.main()
