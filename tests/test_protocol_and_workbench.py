from __future__ import annotations

import socket
import threading
import unittest

from edgecache.errors import EdgeError, PortError
from edgecache.tcp import JsonLineServer, require_loopback_free, rpc
from edgecache.util import JsonLogger


class ProtocolTests(unittest.TestCase):
    def test_json_line_round_trip_and_port_conflict(self) -> None:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        server = JsonLineServer(
            "127.0.0.1",
            port,
            lambda request: {"echo": request.get("value")},
            JsonLogger(None, "test"),
            "test",
        )
        server.start()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            response = rpc("127.0.0.1", port, {"op": "echo", "value": 7})
            self.assertEqual(response["echo"], 7)
            with self.assertRaises(PortError) as caught:
                require_loopback_free("127.0.0.1", port)
            self.assertEqual(caught.exception.code, "E_PORT")
        finally:
            server.request_stop()
            thread.join(timeout=3)

    def test_remote_business_error_is_preserved(self) -> None:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()

        def handler(_request):
            raise EdgeError("E_VERSION", "stale", "request.ledger_version", {"current": 9})

        server = JsonLineServer("127.0.0.1", port, handler, JsonLogger(None, "test"), "test")
        server.start()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with self.assertRaises(EdgeError) as caught:
                rpc("127.0.0.1", port, {"op": "x"})
            self.assertEqual(caught.exception.code, "E_VERSION")
            self.assertEqual(caught.exception.details["current"], 9)
        finally:
            server.request_stop()
            thread.join(timeout=3)

    def test_workbench_model_stable_without_tkinter(self) -> None:
        import tempfile
        from pathlib import Path

        from edgecache.node import NodeCache
        from edgecache.workbench import WorkbenchModel

        from tests.helpers import FakeClock, FakeSource, bootstrap_ledger

        with tempfile.TemporaryDirectory() as temp:
            ledger = bootstrap_ledger(Path(temp) / "ledger.sqlite3")
            clock = FakeClock()
            cache = NodeCache(ledger, "edge-z", FakeSource(ledger), now_ms=clock.now)
            cache.fill(
                path="/assets/theme.css",
                key_values={"tenant": "public", "encoding": "identity"},
                expected_version=1,
            )
            view_a = WorkbenchModel(ledger).build()
            view_b = WorkbenchModel(ledger).build()
            self.assertEqual(view_a["entries"], view_b["entries"])
            self.assertEqual(view_a["content"], sorted(view_a["content"], key=lambda item: item["path"]))
            self.assertEqual(len(view_a["ledger_history"]), 1)


if __name__ == "__main__":
    unittest.main()
