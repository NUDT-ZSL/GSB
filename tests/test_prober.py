from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from lbhealth.config import parse_config
from lbhealth.prober import LoopbackProber

from .helpers import BackendProcess, base_raw, free_port


class RealProberTests(unittest.TestCase):
    def _config(self, members: list[dict]) -> object:
        raw = base_raw()
        raw["pool"]["interval_ms"] = 600
        raw["pool"]["timeout_ms"] = 90
        raw["pool"]["members"] = members
        return parse_config(raw)

    def test_http_success_and_http_status_failure_are_real_sockets(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            with BackendProcess(
                temp_path,
                "http",
                {"version": 1, "default": "ok", "actions": [], "slow_ms": 0},
            ) as backend:
                config = self._config(
                    [
                        {
                            "id": "alpha",
                            "address": "127.0.0.1",
                            "port": backend.port,
                            "protocol": "http",
                            "weight": 1,
                            "probe_path": "/health",
                        }
                    ]
                )
                result = LoopbackProber(config).probe_all(1)[0]
                self.assertTrue(result.ok)
                self.assertEqual(result.summary, "HTTP 200; ok")
                self.assertGreaterEqual(result.end_mono_ms, result.start_mono_ms)
                self.assertIsNone(result.failure_code)
                backend.behavior_path.write_text(
                    '{"version":1,"default":"fail","actions":[],"slow_ms":0}\n',
                    encoding="utf-8",
                )
                failed = LoopbackProber(config).probe_all(2)[0]
                self.assertFalse(failed.ok)
                self.assertEqual(failed.failure_code, "http_status")
                self.assertIn("HTTP 503", failed.summary)

    def test_socket_timeout_comes_from_real_slow_backend(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            with BackendProcess(
                temp_path,
                "http",
                {"version": 1, "default": "slow", "actions": [], "slow_ms": 350},
            ) as backend:
                config = self._config(
                    [
                        {
                            "id": "slow",
                            "address": "127.0.0.1",
                            "port": backend.port,
                            "protocol": "http",
                            "weight": 1,
                            "probe_path": "/health",
                        }
                    ]
                )
                started = time.monotonic()
                result = LoopbackProber(config).probe_all(1)[0]
                elapsed_ms = (time.monotonic() - started) * 1000
                self.assertFalse(result.ok)
                self.assertEqual(result.failure_code, "timeout")
                self.assertGreaterEqual(result.duration_ms, 60)
                self.assertLess(result.duration_ms, 300)
                self.assertLess(elapsed_ms, 300)

    def test_tcp_pong_and_connection_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            with BackendProcess(
                temp_path,
                "tcp",
                {"version": 1, "default": "ok", "actions": [], "slow_ms": 0},
            ) as backend:
                refused_port = free_port()
                config = self._config(
                    [
                        {
                            "id": "tcp-ok",
                            "address": "127.0.0.1",
                            "port": backend.port,
                            "protocol": "tcp",
                            "weight": 1,
                        },
                        {
                            "id": "refused",
                            "address": "127.0.0.1",
                            "port": refused_port,
                            "protocol": "tcp",
                            "weight": 1,
                        },
                    ]
                )
                results = LoopbackProber(config).probe_all(1)
                self.assertTrue(results[0].ok)
                self.assertEqual(results[0].summary, "TCP PONG")
                self.assertFalse(results[1].ok)
                self.assertEqual(results[1].failure_code, "refused")


if __name__ == "__main__":
    unittest.main()
