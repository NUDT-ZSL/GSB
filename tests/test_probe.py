from __future__ import annotations

import socket
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from failover_console.models import ProbeLimits
from failover_console.probe import (
    ERROR,
    SUCCESS,
    TIMEOUT,
    UNREACHABLE,
    ProbeRunner,
    RealProbeTransport,
)

from .helpers import (
    AlwaysErrorTransport,
    BlockingSuccessTransport,
    RetryThenSuccessTransport,
    instance,
    load_scene,
    scene_data,
    service,
)


class _Handler(BaseHTTPRequestHandler):
    mode = "ok"

    def do_GET(self) -> None:  # noqa: N802
        if self.mode == "timeout":
            time.sleep(0.15)
        status = 200 if self.mode == "ok" else 503
        body = b"{}"
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: object) -> None:
        return


class ProbeTests(unittest.TestCase):
    def test_real_transport_distinguishes_four_conclusions(self) -> None:
        transport = RealProbeTransport()
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            _Handler.mode = "ok"
            self.assertEqual(transport.probe(port, 100).conclusion, SUCCESS)
            _Handler.mode = "error"
            self.assertEqual(transport.probe(port, 100).conclusion, ERROR)
            _Handler.mode = "timeout"
            self.assertEqual(transport.probe(port, 40).conclusion, TIMEOUT)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        closed_port = sock.getsockname()[1]
        sock.close()
        self.assertEqual(transport.probe(closed_port, 80).conclusion, UNREACHABLE)

    def test_budget_and_service_quota_are_never_exceeded(self) -> None:
        transport = BlockingSuccessTransport()
        data = scene_data(
            [
                service(
                    "alpha",
                    [instance("a1", "primary", 49301), instance("a2", "standby", 49302)],
                ),
                service(
                    "beta",
                    [instance("b1", "primary", 49311), instance("b2", "standby", 49312)],
                ),
                service(
                    "gamma",
                    [instance("g1", "primary", 49321), instance("g2", "standby", 49322)],
                ),
            ],
            global_concurrency=3,
            service_quota=1,
        )
        scene = load_scene(data)
        batch = ProbeRunner(transport).run(
            scene.instances(),
            ProbeLimits(3, 1),
            {},
            run_id="run-budget",
            round_no=1,
        )
        self.assertLessEqual(batch.peak_global, 3)
        self.assertGreaterEqual(batch.peak_global, 2)
        for peak in batch.peak_by_service.values():
            self.assertLessEqual(peak, 1)
        self.assertTrue(all(result.success for result in batch.results))
        self.assertLessEqual(transport.peak, 3)

    def test_retries_are_individual_attempts_and_do_not_overwrite(self) -> None:
        data = scene_data(
            [
                service(
                    "svc",
                    [
                        instance(
                            "svc-a",
                            "primary",
                            49401,
                            retries=1,
                        )
                    ],
                )
            ]
        )
        scene = load_scene(data)
        attempts: list[object] = []
        failures: list[object] = []
        runner = ProbeRunner(
            RetryThenSuccessTransport(),
            on_attempt=attempts.append,
            on_exhausted=failures.append,
        )
        batch = runner.run(
            scene.instances(),
            scene.probe_limits,
            {},
            run_id="run-retry",
            round_no=1,
        )
        self.assertEqual(len(attempts), 2)
        self.assertEqual(batch.results[0].attempts[0].conclusion, UNREACHABLE)
        self.assertEqual(batch.results[0].attempts[1].conclusion, SUCCESS)
        self.assertEqual(batch.results[0].final_conclusion, SUCCESS)
        self.assertEqual(failures, [])

    def test_exhausted_retries_keep_each_failed_attempt(self) -> None:
        scene = load_scene(
            scene_data(
                [
                    service(
                        "svc",
                        [instance("svc-a", "primary", 49501, retries=2)],
                    )
                ]
            )
        )
        attempts: list[object] = []
        failures: list[object] = []
        runner = ProbeRunner(
            AlwaysErrorTransport(),
            on_attempt=attempts.append,
            on_exhausted=failures.append,
        )
        batch = runner.run(
            scene.instances(),
            scene.probe_limits,
            {},
            run_id="run-fail",
            round_no=1,
        )
        self.assertEqual(len(attempts), 3)
        self.assertEqual(len(failures), 1)
        self.assertEqual(
            [item.conclusion for item in batch.results[0].attempts], [ERROR, ERROR, ERROR]
        )
        self.assertEqual(batch.results[0].final_conclusion, ERROR)


if __name__ == "__main__":
    unittest.main()
