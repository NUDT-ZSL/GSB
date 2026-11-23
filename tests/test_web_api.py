from __future__ import annotations

import json
import tempfile
import threading
import unittest
from urllib.request import Request, urlopen

from vocalforge.service import LabService
from vocalforge.web import create_server


class WebApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.service = LabService(tempfile.mkdtemp(prefix="vf-web-"))
        cls.server = create_server(cls.service, host="127.0.0.1", port=0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def _get(self, path: str) -> dict:
        with urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))

    def _post(self, path: str, payload: dict) -> dict:
        request = Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))

    def test_health_and_state(self) -> None:
        health = self._get("/api/health")
        self.assertTrue(health["ok"])
        state = self._get("/api/state")
        self.assertEqual(state["dataset"]["total_frames"], 460)
        self.assertIn("snapshot", state)
        self.assertIn("error_spectrum", state)
        with urlopen(f"http://127.0.0.1:{self.port}/", timeout=10) as response:
            html = response.read().decode("utf-8")
        self.assertIn("VocalForge", html)
        with urlopen(f"http://127.0.0.1:{self.port}/app.js", timeout=10) as response:
            javascript = response.read().decode("utf-8")
        self.assertIn("loadState", javascript)

    def test_precision_switch_and_injection(self) -> None:
        result = self._post("/api/precision", {"profile": "float32"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["active_profile"], "float32")
        injection = self._post("/api/inject", {"stage": "ref_missing"})
        self.assertFalse(injection["ok"])
        self.assertEqual(injection["error"]["code"], "E_REF_MISSING")

    def test_consistency_endpoint(self) -> None:
        result = self._get("/api/consistency")
        self.assertTrue(result["ok"], result)


if __name__ == "__main__":
    unittest.main()
