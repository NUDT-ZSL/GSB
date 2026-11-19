from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from latentflow.web import create_server


class HTTPAppTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.server = create_server("127.0.0.1", 0, Path(self.temp.name))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.base = f"http://{host}:{port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temp.cleanup()

    def request_json(
        self, path: str, body: dict | None = None
    ) -> tuple[int, dict]:
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            self.base + path,
            data=data,
            headers={"Content-Type": "application/json"},
            method="GET" if body is None else "POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_health_static_and_demo_endpoint(self) -> None:
        status, health = self.request_json("/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(health["ok"])
        with urllib.request.urlopen(self.base + "/", timeout=10) as response:
            html = response.read().decode("utf-8")
        self.assertIn("LatentFlow", html)

        payload = {
            "config": {
                "prompt": "http local demo",
                "seed": 9,
                "width": 48,
                "height": 32,
                "frames": 4,
                "steps": 2,
                "block_size": 2,
                "latency_budget_ms": 1000,
            },
            "compare_block_size": 3,
        }
        status, result = self.request_json("/api/demo/run", payload)
        self.assertEqual(status, 200)
        self.assertTrue(result["ok"])
        self.assertTrue(result["completion"]["consistency"]["equal"])
        job_id = result["start"]["job"]["job_id"]
        ready_status, ready = self.request_json(f"/api/jobs/{job_id}/ready")
        self.assertEqual(ready_status, 200)
        self.assertEqual(ready["ready_chunks"], 2)
        self.assertEqual(ready["ready_frames"], 4)

    def test_http_business_error_code(self) -> None:
        status, result = self.request_json(
            "/api/jobs",
            {"config": {"anchor_strength": 2.0}},
        )
        self.assertEqual(status, 400)
        self.assertEqual(result["error"]["code"], "E_CONFIG_INVALID")


if __name__ == "__main__":
    unittest.main()
