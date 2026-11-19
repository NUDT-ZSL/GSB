from __future__ import annotations

import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.request import Request, urlopen

from annotation_app.service import WorkbenchService
from annotation_app.web import WorkbenchHandler

from tests.helpers import ROOT


class ServiceWebTests(unittest.TestCase):
    def test_service_views_and_edit(self):
        service = WorkbenchService(ROOT)
        v2 = service.state("v2")
        v1 = service.state("v1")
        line = service.state("line")
        self.assertEqual(v2["current_format"], "v2")
        self.assertEqual(v1["view_content"]["version"], "v1")
        self.assertIn("#LEDGER-LINE|1", line["view_content"])
        result = service.apply_edit(
            "change_argument_role",
            {"argument_id": "a1", "role": "employer"},
        )
        self.assertEqual(result["status"], "applied")
        self.assertEqual(result["state"]["audit"][-1]["operation"], "change_argument_role")
        service.reset()
        self.assertEqual(service.state()["audit"], [])

    def test_local_http_endpoints(self):
        service = WorkbenchService(ROOT)
        handler = type(
            "TestHandler",
            (WorkbenchHandler,),
            {"service": service},
        )
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            with urlopen(base + "/", timeout=5) as response:
                html = response.read().decode("utf-8")
            self.assertIn("EventGraph", html)
            with urlopen(base + "/api/state?view=line", timeout=5) as response:
                state = json.loads(response.read().decode("utf-8"))
            self.assertEqual(state["status"], "ok")
            self.assertIn("#LEDGER-LINE|1", state["state"]["view_content"])
            request = Request(
                base + "/api/view",
                data=json.dumps({"view": "v1"}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request, timeout=5) as response:
                switched = json.loads(response.read().decode("utf-8"))
            self.assertEqual(switched["state"]["view_content"]["version"], "v1")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()

