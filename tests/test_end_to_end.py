from __future__ import annotations

import subprocess
import sys
import threading

from werkzeug.serving import make_server

from gateway.app import create_app

from .conftest import ROOT


def test_local_flask_gateway_and_upstream_replay(service, upstream_server):
    app = create_app(service=service)
    server = make_server("127.0.0.1", 18080, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        completed = subprocess.run(
            [
                sys.executable,
                "scripts/replay.py",
                "--base-url",
                "http://127.0.0.1:18080",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert '"failures": 0' in completed.stdout
        assert "PASS tenant-bucket-exhausted" in completed.stdout
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_admin_http_update_is_atomic(service, upstream_server):
    app = create_app(service=service)
    server = make_server("127.0.0.1", 18080, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        update = subprocess.run(
            [
                sys.executable,
                "scripts/update_config.py",
                "--base-url",
                "http://127.0.0.1:18080",
                "--config",
                "samples/config_v2.json",
                "--expected-current-version",
                "1",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert update.returncode == 0, update.stdout + update.stderr
        assert '"version":2' in update.stdout
        assert service.store.current_version() == 2
    finally:
        server.shutdown()
        thread.join(timeout=5)
