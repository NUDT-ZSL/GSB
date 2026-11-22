from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from quantlab.pipeline import Workbench
from quantlab.server import WorkbenchHandler

from tests.helpers import make_config


class ServerTests(unittest.TestCase):
    def test_local_server_serves_dashboard_and_static_assets(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = make_config(root)
            Workbench(config).run(clean=True)
            old_artifacts = WorkbenchHandler.artifacts_dir
            WorkbenchHandler.artifacts_dir = config.artifacts_dir
            server = ThreadingHTTPServer(("127.0.0.1", 0), WorkbenchHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_address[1]}"
                with urllib.request.urlopen(f"{base}/api/dashboard") as response:
                    dashboard = json.loads(response.read().decode("utf-8"))
                self.assertEqual(response.status, 200)
                self.assertTrue(dashboard["summary"]["consistency_ok"])
                with urllib.request.urlopen(f"{base}/") as response:
                    html = response.read().decode("utf-8")
                self.assertIn("QuantTrace Lab", html)
                with urllib.request.urlopen(f"{base}/static/app.js") as response:
                    app_js = response.read().decode("utf-8")
                self.assertIn("renderDashboard", app_js)
            finally:
                server.shutdown()
                server.server_close()
                WorkbenchHandler.artifacts_dir = old_artifacts


if __name__ == "__main__":
    unittest.main()
