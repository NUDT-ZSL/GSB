"""pytest 共享夹具：确定性工作区、模拟服务子进程、采集子进程驱动。"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
import requests

from recon import generator

ROOT = Path(__file__).resolve().parent.parent
PYTHON = sys.executable


def _wait_port(host: str, port: int, timeout: float = 10.0,
               expect_closed: bool = False) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(0.2)
        try:
            sock.connect((host, port))
            open_ok = True
        except OSError:
            open_ok = False
        finally:
            sock.close()
        if expect_closed and not open_ok:
            return True
        if not expect_closed and open_ok:
            try:
                if requests.get(f"http://{host}:{port}/health", timeout=2).ok:
                    return True
            except requests.RequestException:
                pass
        time.sleep(0.1)
    return False


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class ServerHandle:
    def __init__(self, proc: subprocess.Popen, port: int, rules_path: str,
                 profile_path: str, token: str = "shutdown-token"):
        self.proc = proc
        self.port = port
        self.rules_path = rules_path
        self.profile_path = profile_path
        self.token = token

    def shutdown(self) -> dict:
        resp = requests.get(
            f"http://127.0.0.1:{self.port}/shutdown",
            params={"token": self.token}, timeout=5,
        )
        data = resp.json()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        return data

    def profile(self) -> dict:
        with open(self.profile_path, "r", encoding="utf-8") as fh:
            return json.load(fh)


class Workspace:
    def __init__(self, path: Path, port: int, manifest: dict):
        self.path = path
        self.port = port
        self.manifest = manifest

    def config(self, name: str = "base") -> str:
        return self.manifest["configs"][name]

    @property
    def data_dir(self) -> Path:
        return self.path / "data"

    def start_server(self, scenario: str | None = None) -> ServerHandle:
        rules = (self.manifest["scenario_rules"][scenario]
                 if scenario else self.manifest["rules_path"])
        log = open(self.path / "server.log", "w", encoding="utf-8")
        proc = subprocess.Popen(
            [PYTHON, "-m", "recon.mock_server", rules, str(self.port)],
            cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        )
        assert _wait_port("127.0.0.1", self.port), "模拟服务未能在超时内就绪"
        return ServerHandle(proc, self.port, rules,
                            self.manifest["summary"] and self.path / "data" / "server_profile.json")

    def run_collector(self, config_name: str = "base",
                      failure_hook: str | None = None,
                      env: dict | None = None) -> subprocess.CompletedProcess:
        cmd = [PYTHON, "-m", "recon.cli", "--config", self.config(config_name)]
        if failure_hook:
            cmd += ["--failure-hook", failure_hook]
        merged_env = dict(os.environ)
        if env:
            merged_env.update(env)
        return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                              env=merged_env, timeout=120)

    def write_control(self, content: str = "stop") -> None:
        with open(self.manifest["configs"]["base"], "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
        with open(cfg["control_file"], "w", encoding="utf-8") as fh:
            fh.write(content)

    def remove_control(self) -> None:
        with open(self.manifest["configs"]["base"], "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
        p = cfg["control_file"]
        if os.path.exists(p):
            os.remove(p)

    def latest_report(self, aborted: bool = False) -> dict:
        pattern = "report_*.aborted.json" if aborted else "report_*.json"
        files = [p for p in (self.path / "reports").glob(pattern)
                 if not (aborted is False and p.name.endswith(".aborted.json"))]
        assert files, "未找到报告文件"
        latest = max(files, key=lambda p: p.stat().st_mtime)
        with open(latest, "r", encoding="utf-8") as fh:
            return json.load(fh)

    def truth(self) -> dict:
        with open(self.data_dir / "truth.json", "r", encoding="utf-8") as fh:
            return json.load(fh)


@pytest.fixture
def workspace(tmp_path) -> Workspace:
    port = _free_port()
    manifest = generator.generate(str(tmp_path), seed=42, port=port)
    return Workspace(tmp_path, port, manifest)


@pytest.fixture
def server(workspace):
    handle = workspace.start_server()
    yield handle
    if handle.proc.poll() is None:
        try:
            handle.shutdown()
        except Exception:
            handle.proc.kill()
    assert _wait_port("127.0.0.1", workspace.port, expect_closed=True), \
        "服务停止后端口仍可连接"
