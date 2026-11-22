from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request

from hotgray.server import make_server

from .support import ROOT, TempWorkspace, warmed_engine


class WorkbenchTests(unittest.TestCase):
    def test_http_workbench_status_is_stable_and_has_no_wall_clock_header(self) -> None:
        with TempWorkspace() as workspace:
            engine = warmed_engine(workspace.path)
            engine.advance_stage(at_ms=1200, level=1)
            server = make_server(engine.store.db_path, host="127.0.0.1", port=0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            port = server.server_address[1]
            base = f"http://127.0.0.1:{port}"
            try:
                with urllib.request.urlopen(f"{base}/api/status", timeout=5) as response:
                    self.assertIsNone(response.headers.get("Date"))
                    payload = json.loads(response.read().decode("utf-8"))
                self.assertEqual(payload["round"]["status"], "gray")
                self.assertEqual(
                    [item["replica_id"] for item in payload["replicas"]],
                    ["replica-a", "replica-b", "replica-control"],
                )
                self.assertTrue(payload["version_bindings"]["2"]["shard_chain"])
                for replica in payload["replicas"]:
                    self.assertIn("shard_chain", replica)
                    self.assertIn("invalidated", replica)
                    self.assertIn("reusable", replica)
                with urllib.request.urlopen(f"{base}/api/status", timeout=5) as response:
                    second = json.loads(response.read().decode("utf-8"))
                self.assertEqual(payload, second)
                with urllib.request.urlopen(f"{base}/", timeout=5) as response:
                    html = response.read().decode("utf-8")
                    self.assertIsNone(response.headers.get("Date"))
                self.assertIn("HotGray", html)
                with urllib.request.urlopen(f"{base}/app.js", timeout=5) as response:
                    self.assertIn("renderShardChain", response.read().decode("utf-8"))
                with urllib.request.urlopen(f"{base}/styles.css", timeout=5) as response:
                    self.assertIn("--teal", response.read().decode("utf-8"))
                with urllib.request.urlopen(f"{base}/api/error-catalog", timeout=5) as response:
                    catalog = json.loads(response.read().decode("utf-8"))
                codes = [item["code"] for item in catalog["errors"]]
                self.assertEqual(codes, sorted(codes))
                self.assertIn("HG1003", codes)
                self.assertIn("HG1004", codes)
                self.assertIn("HG1005", codes)
                self.assertIn("HG1006", codes)
                self.assertIn("HG1007", codes)
                self.assertIn("HG1008", codes)
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    urllib.request.urlopen(f"{base}/unknown", timeout=5)
                self.assertEqual(raised.exception.code, 404)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_business_time_sources_are_not_read(self) -> None:
        forbidden = (
            "time.time(",
            "time.monotonic(",
            "datetime.now(",
            "datetime.utcnow(",
        )
        for path in sorted((ROOT / "hotgray").glob("*.py")):
            text = path.read_text(encoding="utf-8")
            for token in forbidden:
                self.assertNotIn(token, text, f"{path} 读取了系统墙钟：{token}")


if __name__ == "__main__":
    unittest.main()
