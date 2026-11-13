"""在临时端口启动两版服务，供命令行演示与自动化测试使用。"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class RunningService:
    version: str
    base_url: str
    port: int
    pid: int
    process: subprocess.Popen | None = None

    def stop(self, timeout: float = 5.0) -> None:
        if self.process is None:
            return
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=timeout)
        self.process = None


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for_health(base_url: str, process: subprocess.Popen, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"service exited early with code {process.returncode}")
        try:
            with urllib.request.urlopen(f"{base_url}/health", timeout=0.5) as response:
                if response.status == 200:
                    return json.loads(response.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - 启动期间连接失败属正常
            last_error = exc
        time.sleep(0.05)
    raise RuntimeError(f"service did not become healthy: {last_error}")


def start_service(
    version: str,
    port: int = 0,
    token: str | None = None,
    python: str | None = None,
) -> RunningService:
    """以子进程方式启动 ``python -m authsvc --version <version>``。"""
    chosen_port = port or _free_port()
    ready_file = PROJECT_ROOT / "reports" / f".ready-{version}-{os.getpid()}-{chosen_port}.json"
    ready_file.parent.mkdir(parents=True, exist_ok=True)
    if ready_file.exists():
        ready_file.unlink()

    command = [
        python or sys.executable,
        "-m",
        "authsvc",
        "--version",
        version,
        "--port",
        str(chosen_port),
        "--ready-file",
        str(ready_file),
    ]
    if token:
        command.extend(["--token", token])

    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(PROJECT_ROOT)
    process = subprocess.Popen(
        command,
        cwd=str(PROJECT_ROOT),
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    base_url = f"http://127.0.0.1:{chosen_port}"
    try:
        _wait_for_health(base_url, process)
    except Exception:
        process.terminate()
        raise
    finally:
        if ready_file.exists():
            ready_file.unlink(missing_ok=True)
    return RunningService(
        version=version,
        base_url=base_url,
        port=chosen_port,
        pid=process.pid,
        process=process,
    )
