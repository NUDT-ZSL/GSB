from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class TestServer:
    base_url: str
    db_path: Path
    process: subprocess.Popen
    log_path: Path


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_health(base_url: str, process: subprocess.Popen, log_path: Path) -> None:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"server exited early:\n{log_path.read_text(encoding='utf-8')}")
        try:
            response = httpx.get(f"{base_url}/api/health", timeout=0.7)
            if response.status_code == 200:
                return
        except Exception:
            pass
        time.sleep(0.05)
    raise RuntimeError(f"server health timeout:\n{log_path.read_text(encoding='utf-8')}")


@pytest.fixture
def server(tmp_path: Path) -> TestServer:
    port = _free_port()
    db_path = tmp_path / "workbench.sqlite3"
    log_path = tmp_path / "server.log"
    log = log_path.open("w", encoding="utf-8")
    env = os.environ.copy()
    env.update(
        {
            "PORT": str(port),
            "HOST": "127.0.0.1",
            "PUBLIC_BASE_URL": f"http://127.0.0.1:{port}",
            "AUDIT_DB_PATH": str(db_path),
            "LOG_LEVEL": "warning",
        }
    )
    process = subprocess.Popen(
        [sys.executable, "-m", "app.main"],
        cwd=str(ROOT),
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    base_url = f"http://127.0.0.1:{port}"
    _wait_health(base_url, process, log_path)
    try:
        yield TestServer(base_url=base_url, db_path=db_path, process=process, log_path=log_path)
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        log.close()
