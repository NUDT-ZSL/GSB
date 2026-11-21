from __future__ import annotations

import json
import socket
import subprocess
import sys
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from capacity_engine.mock_service import MockConfig, make_handler
from http.server import ThreadingHTTPServer


ROOT = Path(__file__).resolve().parents[1]
SAMPLE_SCENE = ROOT / "samples" / "scene.json"
SAMPLE_MOCK = ROOT / "samples" / "mock_service.json"


@contextmanager
def mock_server(config_path: Path = SAMPLE_MOCK) -> Iterator[int]:
    with config_path.open("r", encoding="utf-8") as handle:
        config = MockConfig(json.load(handle))
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(config))
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def unused_loopback_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def write_scene_with_port(directory: Path, port: int, name: str = "scene.json") -> Path:
    document = json.loads(SAMPLE_SCENE.read_text(encoding="utf-8"))
    document["services"][0]["address"] = f"http://127.0.0.1:{port}"
    path = directory / name
    path.write_text(json.dumps(document, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def write_json(path: Path, payload: Any) -> Path:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def run_cli(*args: str, timeout: float = 90.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "capacity_engine", *args],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def parse_stdout_json(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    return json.loads(result.stdout)


def parse_stderr_json(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    return json.loads(result.stderr)

