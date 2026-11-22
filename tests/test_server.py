from __future__ import annotations

import json
import shutil
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

from ocrlab.server import ApplicationServer
from ocrlab.service import WorkspaceService


class ServerSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(tempfile.mkdtemp(prefix="ocrlab-server-"))
        cls.service = WorkspaceService(
            cls.root / "data",
            cls.root / "state",
            page_count=3,
        )
        cls.server = ApplicationServer(("127.0.0.1", 0), cls.service)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(3)
        shutil.rmtree(cls.root, ignore_errors=True)

    def request(self, path: str, method: str = "GET", body: dict | None = None) -> tuple[int, bytes, str]:
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read(), response.headers.get("Content-Type", "")

    def test_static_state_image_and_run_api(self) -> None:
        status, payload, content_type = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", content_type)
        self.assertIn("GlyphTrace", payload.decode("utf-8"))

        status, payload, _ = self.request("/api/state")
        self.assertEqual(status, 200)
        state = json.loads(payload)["state"]
        self.assertEqual(len(state["pages"]), 3)
        page_id = state["pages"][0]["page_id"]

        status, image, content_type = self.request(f"/api/images/{page_id}.png")
        self.assertEqual(status, 200)
        self.assertEqual(content_type, "image/png")
        self.assertTrue(image.startswith(b"\x89PNG\r\n\x1a\n"))

        status, payload, _ = self.request("/api/run", method="POST", body={})
        self.assertEqual(status, 200)
        result = json.loads(payload)["result"]
        self.assertTrue(result["selfcheck"]["ok"])
        self.assertEqual(len(result["pages"]), 3)

        status, payload, _ = self.request("/api/state")
        state = json.loads(payload)["state"]
        self.assertIn("report_checksum", state["report"])
        self.assertEqual(state["snapshots"]["last_committed_stage"], 4)
        self.assertEqual(set(state["snapshots"]["stages"]), {"1", "2", "3", "4"})

    def test_demo_and_edit_api(self) -> None:
        status, payload, _ = self.request("/api/demo/injection", method="POST", body={})
        self.assertEqual(status, 200)
        result = json.loads(payload)["result"]
        self.assertGreaterEqual(result["injection_count"], 3)

        status, payload, _ = self.request("/api/state")
        state = json.loads(payload)["state"]
        page = state["pages"][-1]
        lines = [
            {"text": line["text"], "truth_box": line["truth_box"]}
            for line in page["lines"]
        ]
        lines[0]["text"] = "AB12"
        lines[0]["truth_box"] = [25, 20, 25 + 4 * 13, 36]
        status, payload, _ = self.request(
            f"/api/pages/{page['page_id']}",
            method="PUT",
            body={"lines": lines},
        )
        self.assertEqual(status, 200)
        edited = json.loads(payload)["page"]
        self.assertEqual(edited["lines"][0]["text"], "AB12")


if __name__ == "__main__":
    unittest.main()

