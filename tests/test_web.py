from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
import urllib.request
from urllib.error import URLError

from corpus_dedup_workbench.web import WorkbenchHTTPServer
from tests.helpers import make_service


class WebTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.service = make_service(self.temp.name)
        self.server = WorkbenchHTTPServer(("127.0.0.1", 0), self.service)
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            daemon=True,
        )
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.temp.cleanup()

    def fetch_json(self, path: str, body: dict | None = None) -> dict:
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        last_error: Exception | None = None
        for attempt in range(4):
            request = urllib.request.Request(
                self.base_url + path,
                data=data,
                headers=headers,
            )
            try:
                with urllib.request.urlopen(request, timeout=10) as response:
                    return json.loads(response.read().decode("utf-8"))
            except URLError as exc:
                last_error = exc
                time.sleep(0.05 * (attempt + 1))
        raise AssertionError(f"HTTP request failed: {last_error}")

    def test_index_dashboard_and_query(self) -> None:
        with urllib.request.urlopen(self.base_url + "/", timeout=5) as response:
            html = response.read().decode("utf-8")
        self.assertIn("语料去重诊断工作台", html)
        self.assertIn("分片级统计", html)
        dashboard = self.fetch_json("/api/dashboard")
        self.assertTrue(dashboard["ok"])
        self.assertEqual(dashboard["totals"]["doc_count"], 72)
        self.assertEqual(set(dashboard["shard_stats"]), {
            "shard-00",
            "shard-01",
            "shard-02",
            "shard-03",
        })
        query = self.fetch_json("/api/query?doc_id=doc-000000&budget_ms=50")
        self.assertTrue(query["ok"])
        self.assertTrue(query["consistency"]["checked"])
        status = self.fetch_json("/api/status")
        self.assertGreaterEqual(len(status["diagnostics"]["latencies"]), 1)

    def test_config_post_reports_cache_invalidation(self) -> None:
        response = self.fetch_json(
            "/api/config",
            {"candidate_window": 2},
        )
        self.assertTrue(response["ok"])
        transition = response["transition"]
        self.assertIn("candidate_window", transition["changed_fields"])
        self.assertEqual(len(transition["invalidated_entries"]), 4)
        self.assertTrue(transition["affected_docs"])


if __name__ == "__main__":
    unittest.main()

