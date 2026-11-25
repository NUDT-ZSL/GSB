from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from generate_sample import write_sample
from image_diag.service import ImageDiagService
from image_diag.web import DashboardHandler


class DemoAndWebTests(unittest.TestCase):
    def test_incremental_invalidation_and_recovery(self) -> None:
        with tempfile.TemporaryDirectory(prefix="image-diag-demo-") as temporary:
            root = Path(temporary)
            sample_dir = root / "sample"
            write_sample(sample_dir)
            service = ImageDiagService(root / "runtime", sample_dir)
            incremental = service.demo_incremental()
            self.assertEqual(incremental["recomputed_regions"], ["R2"])
            self.assertEqual(incremental["reused_regions"], ["R1", "R3"])
            self.assertTrue(incremental["incremental_equal_full"])
            self.assertEqual(incremental["cache_events"]["recomputed"], 5)
            self.assertEqual(incremental["cache_events"]["reused"], 10)

            style_revision = service.demo_style_revision()
            self.assertTrue(style_revision["upstream_reused"])
            self.assertTrue(style_revision["downstream_recomputed"])
            self.assertTrue(style_revision["revision_equal_full"])
            self.assertEqual(style_revision["cache_events"]["reused"], 9)
            self.assertEqual(style_revision["cache_events"]["recomputed"], 6)

            rollback = service.demo_rollback()
            self.assertEqual(rollback["error"]["code"], "E_SNAPSHOT_ROLLBACK")
            self.assertTrue(rollback["head_unchanged"])

            busy = service.demo_busy()
            self.assertEqual(busy["error"]["code"], "E_STAGE_BUSY")
            self.assertTrue(busy["cross_process"])

            recovery = service.demo_recovery(fail_after=6)
            self.assertEqual(recovery["terminated_process_return_code"], 42)
            self.assertEqual(recovery["checkpoint_count_before_resume"], 6)
            self.assertEqual(recovery["resumed_checkpoint_events"], 6)
            self.assertTrue(recovery["recovery_equal_full"])

    def test_http_dashboard_api(self) -> None:
        with tempfile.TemporaryDirectory(prefix="image-diag-http-") as temporary:
            root = Path(temporary)
            sample_dir = root / "sample"
            write_sample(sample_dir)
            service = ImageDiagService(root / "runtime", sample_dir)

            class Handler(DashboardHandler):
                pass

            Handler.service = service
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_address[1]}"
                with urllib.request.urlopen(f"{base}/api/state", timeout=5) as response:
                    state = json.loads(response.read())
                self.assertTrue(state["ok"])
                request = urllib.request.Request(
                    f"{base}/api/run",
                    data=json.dumps(
                        {
                            "precision": "float32",
                            "focus_path": "block",
                            "policy": "tolerance_band",
                            "threshold_tolerance": 0.012,
                            "error_tolerance": 0.01,
                        }
                    ).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=10) as response:
                    result = json.loads(response.read())
                self.assertTrue(result["ok"])
                self.assertTrue(result["block_match"]["bitwise_equal"])
                self.assertIn("images", result)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
