from __future__ import annotations

import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

from flightfusion.app import DemoController, _make_handler


def test_dashboard_state_and_demo_trigger(tmp_path):
    controller = DemoController(
        cache_dir=tmp_path / "app-cache",
        artifact_path=tmp_path / "live_demo.json",
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(controller))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with urllib.request.urlopen(base + "/", timeout=20) as response:
            html = response.read().decode("utf-8")
        assert "运行完整演示" in html
        assert "姿态曲线与融合残差" in html

        with urllib.request.urlopen(base + "/api/state", timeout=40) as response:
            report = json.loads(response.read().decode("utf-8"))
        assert report["schema"] == "flightfusion.demo.v1"
        assert report["snapshot"]["active_version"] == 5
        assert report["acceptance"]["incremental_equals_full"] is True

        request = urllib.request.Request(
            base + "/api/demo",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=40) as response:
            refreshed = json.loads(response.read().decode("utf-8"))
        assert refreshed["initial"]["output"]["stats"]["sample_count"] == 401
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

