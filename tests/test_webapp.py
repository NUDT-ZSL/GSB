from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from tracker_app.data_gen import generate_dataset, write_dataset
from tracker_app.engine import TrackingEngine
from tracker_app.webapp import AppState, make_handler

from tests.helpers import small_config, write_config


class WebAppTests(unittest.TestCase):
    def test_status_and_http_endpoints(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            temp = Path(temp_name)
            config = small_config()
            config["paths"]["runtime_dir"] = "runtime"
            config["paths"]["output_dir"] = "artifacts/demo/output"
            config["paths"]["data_dir"] = "artifacts/demo/data"
            config_path = write_config(config, temp / "config.json")
            dataset = generate_dataset(config)
            write_dataset(dataset, temp / "artifacts" / "demo" / "data")
            engine = TrackingEngine(
                config,
                base_dir=temp,
                runtime_dir=temp / "runtime",
                output_dir=temp / "artifacts" / "demo" / "output",
            )
            engine.run_dataset(
                dataset["detections"],
                dataset["truth"],
                dataset_manifest=dataset["manifest"],
            )
            app_state = AppState(temp, str(config_path))
            status = app_state.status("seq-000", 12)
            self.assertTrue(status["available"])
            self.assertEqual(status["selected"]["frame"], 12)
            self.assertGreaterEqual(len(status["selected"]["detections"]), 1)
            self.assertIn("mota", status["selected"]["metrics"])

            server = ThreadingHTTPServer(
                ("127.0.0.1", 0),
                make_handler(app_state),
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base_url = f"http://127.0.0.1:{server.server_address[1]}"
                with urllib.request.urlopen(base_url + "/") as response:
                    html = response.read().decode("utf-8")
                self.assertIn("TraceLab", html)
                with urllib.request.urlopen(
                    base_url + "/api/status?seq_id=seq-000&frame=12"
                ) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                self.assertTrue(payload["available"])
                self.assertEqual(payload["selected"]["frame"], 12)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
