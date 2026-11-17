from __future__ import annotations

import json
import socket
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

from failover_console.codes import BusinessCode
from failover_console.engine import FailoverEngine

from .helpers import instance, load_scene, scene_data, service


class _Health(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        body = b'{"status":"ok"}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: object) -> None:
        return


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class EndToEndTests(unittest.TestCase):
    def test_real_loopback_http_probe_switches_and_replays(self) -> None:
        ports = [_free_port(), _free_port(), _free_port()]
        servers: list[ThreadingHTTPServer] = []
        threads: list[threading.Thread] = []
        for port in ports[1:]:
            server = ThreadingHTTPServer(("127.0.0.1", port), _Health)
            server.daemon_threads = True
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            servers.append(server)
            threads.append(thread)
        data = scene_data(
            [
                service(
                    "live",
                    [
                        instance("live-a", "primary", ports[0]),
                        instance("live-b", "standby", ports[1]),
                        instance("live-c", "standby", ports[2]),
                    ],
                    cooldown_ms=60_000,
                )
            ],
            faults=[{"instance_id": "live-a", "kind": "unreachable"}],
            global_concurrency=3,
            service_quota=2,
        )
        scene = load_scene(data)
        try:
            with TemporaryDirectory() as td:
                db = Path(td) / "live.sqlite"
                with FailoverEngine(scene, db) as engine:
                    result = engine.run_cycle(now_epoch_ms_override=12_000_000)
                    self.assertIn(BusinessCode.SWITCHED.value, result.codes)
                    self.assertEqual(engine.runtime["live-b"].role, "primary")
                    self.assertTrue(engine.runtime["live-a"].isolated)
                    replay = engine.replay_latest()
                    self.assertTrue(replay.ok, [item.as_dict() for item in replay.differences])
        finally:
            for server in servers:
                server.shutdown()
            for server in servers:
                server.server_close()
            for thread in threads:
                thread.join(timeout=2)

    def test_cli_scene_corruption_returns_e_scene_and_writes_log(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with TemporaryDirectory() as td:
            log = Path(td) / "log.jsonl"
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "failover_console",
                    "validate",
                    "--scene",
                    "scenes/sample_scene.json",
                    "--log",
                    str(log),
                    "--inject",
                    "scene-corruption",
                ],
                cwd=root,
                text=True,
                capture_output=True,
                timeout=20,
                check=False,
            )
            self.assertEqual(completed.returncode, 2)
            payload = json.loads(completed.stdout)
            self.assertEqual(payload["code"], BusinessCode.E_SCENE.value)
            self.assertTrue(payload["issues"])
            self.assertGreaterEqual(payload["issues"][0]["line"], 1)
            records = [
                json.loads(line)
                for line in log.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertTrue(any(item["code"] == BusinessCode.E_SCENE.value for item in records))


if __name__ == "__main__":
    unittest.main()
