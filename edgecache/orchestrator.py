from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from .errors import EdgeError, ProtocolError, StateError
from .tcp import require_loopback_free, rpc, wait_until_ready


PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class ProcessHandle:
    name: str
    command: list[str]
    process: subprocess.Popen[bytes]
    stdout_path: Path
    stderr_path: Path
    stdout_handle: Any
    stderr_handle: Any
    graceful: bool | None = None
    forced: bool = False
    exit_code: int | None = None

    def close_files(self) -> None:
        for handle in (self.stdout_handle, self.stderr_handle):
            try:
                handle.close()
            except Exception:
                pass


class LocalOrchestrator:
    def __init__(self, state_dir: str | Path, base_port: int) -> None:
        self.state_dir = Path(state_dir).resolve()
        self.base_port = base_port
        self.log_dir = self.state_dir / "logs"
        self.runtime_dir = self.state_dir / "runtime"
        self.snapshot_dir = self.state_dir / "snapshots"
        self.db_path = self.runtime_dir / "ledger.sqlite3"
        self.processes: list[ProcessHandle] = []
        self.active: dict[str, ProcessHandle] = {}

    @property
    def source_port(self) -> int:
        return self.base_port

    @property
    def node_ports(self) -> dict[str, int]:
        return {
            "edge-a": self.base_port + 1,
            "edge-b": self.base_port + 2,
            "edge-c": self.base_port + 3,
        }

    def prepare(self, reset: bool) -> None:
        if reset and self.state_dir.exists():
            resolved = self.state_dir
            if resolved == Path(resolved.anchor) or resolved == PROJECT_ROOT:
                raise StateError("refusing to remove an unsafe state directory", location=str(resolved))
            shutil.rmtree(resolved)
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)

    def preflight_ports(self) -> None:
        all_ports = [self.source_port, *self.node_ports.values()]
        for port in all_ports:
            if not (1024 <= port <= 65535):
                raise EdgeError(
                    "E_PORT",
                    "demo requires high loopback ports in 1024..65535",
                    location=f"127.0.0.1:{port}",
                    port=port,
                )
            require_loopback_free("127.0.0.1", port)

    def _spawn(self, name: str, command: list[str]) -> ProcessHandle:
        stdout_path = self.log_dir / f"{name}.stdout.log"
        stderr_path = self.log_dir / f"{name}.stderr.log"
        stdout_handle = stdout_path.open("ab")
        stderr_handle = stderr_path.open("ab")
        env = os.environ.copy()
        existing = env.get("PYTHONPATH")
        env["PYTHONPATH"] = str(PROJECT_ROOT) if not existing else str(PROJECT_ROOT) + os.pathsep + existing
        process = subprocess.Popen(
            command,
            cwd=str(PROJECT_ROOT),
            stdout=stdout_handle,
            stderr=stderr_handle,
            stdin=subprocess.DEVNULL,
            env=env,
        )
        handle = ProcessHandle(
            name=name,
            command=command,
            process=process,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            stdout_handle=stdout_handle,
            stderr_handle=stderr_handle,
        )
        self.processes.append(handle)
        self.active[name] = handle
        return handle

    def start_source(self, policy_path: str, content_path: str) -> ProcessHandle:
        command = [
            sys.executable,
            "-m",
            "edgecache.source",
            "--host",
            "127.0.0.1",
            "--port",
            str(self.source_port),
            "--db",
            str(self.db_path),
            "--policy",
            policy_path,
            "--content",
            content_path,
            "--log",
            str(self.log_dir / "source.jsonl"),
        ]
        handle = self._spawn("source", command)
        self._wait_ready(handle, self.source_port)
        return handle

    def start_node(self, node_id: str) -> ProcessHandle:
        port = self.node_ports[node_id]
        command = [
            sys.executable,
            "-m",
            "edgecache.node",
            "--node-id",
            node_id,
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--source-host",
            "127.0.0.1",
            "--source-port",
            str(self.source_port),
            "--db",
            str(self.db_path),
            "--log",
            str(self.log_dir / f"{node_id}.jsonl"),
        ]
        handle = self._spawn(node_id, command)
        self._wait_ready(handle, port)
        return handle

    def _wait_ready(self, handle: ProcessHandle, port: int) -> None:
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            poll = handle.process.poll()
            if poll is not None:
                handle.exit_code = poll
                handle.close_files()
                error = self._read_process_error(handle)
                if error is not None:
                    raise error
                raise ProtocolError(
                    f"process {handle.name} exited before readiness",
                    location=f"127.0.0.1:{port}",
                    exit_code=poll,
                )
            try:
                rpc("127.0.0.1", port, {"op": "ping"}, timeout=0.5)
                return
            except EdgeError:
                time.sleep(0.05)
        self.kill(handle.name, reason="readiness_timeout")
        raise ProtocolError(
            "service did not become ready before timeout",
            location=f"127.0.0.1:{port}",
        )

    def _read_process_error(self, handle: ProcessHandle) -> EdgeError | None:
        try:
            lines = handle.stderr_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return None
        for line in reversed(lines):
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            error = payload.get("error") if isinstance(payload, dict) else None
            if isinstance(error, dict) and error.get("code"):
                return EdgeError(
                    code=str(error["code"]),
                    message=str(error.get("message", "process startup failed")),
                    location=error.get("location"),
                    details=dict(error.get("details") or {}),
                )
        return None

    def start_all(self, policy_path: str, content_path: str) -> None:
        self.start_source(policy_path, content_path)
        for node_id in self.node_ports:
            self.start_node(node_id)

    def graceful_shutdown(self, name: str) -> dict[str, Any]:
        handle = self.active.get(name)
        if handle is None:
            return {"name": name, "graceful": False, "reason": "not_running"}
        port = self.source_port if name == "source" else self.node_ports[name]
        graceful = False
        try:
            rpc("127.0.0.1", port, {"op": "shutdown"}, timeout=5)
            try:
                code = handle.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                handle.process.kill()
                code = handle.process.wait(timeout=5)
                handle.forced = True
            graceful = not handle.forced
            handle.exit_code = code
        except EdgeError:
            if handle.process.poll() is None:
                handle.process.kill()
                handle.process.wait(timeout=5)
                handle.forced = True
            else:
                handle.exit_code = handle.process.returncode
        handle.graceful = graceful
        handle.close_files()
        self.active.pop(name, None)
        return {
            "name": name,
            "graceful": graceful,
            "forced": handle.forced,
            "exit_code": handle.exit_code,
        }

    def kill(self, name: str, reason: str = "forced_termination") -> dict[str, Any]:
        handle = self.active.get(name)
        if handle is None:
            return {"name": name, "graceful": False, "reason": "not_running"}
        if handle.process.poll() is None:
            handle.process.kill()
        try:
            code = handle.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            code = handle.process.poll()
        handle.exit_code = code
        handle.forced = True
        handle.graceful = False
        handle.close_files()
        self.active.pop(name, None)
        return {
            "name": name,
            "graceful": False,
            "forced": True,
            "exit_code": code,
            "reason": reason,
            "exit_report_generated": False,
        }

    def shutdown_all(self) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for name in reversed(list(self.active)):
            results.append(self.graceful_shutdown(name))
        return results

    def kill_all(self) -> list[dict[str, Any]]:
        return [self.kill(name) for name in list(reversed(list(self.active)))]

    def process_report(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for handle in self.processes:
            items.append(
                {
                    "name": handle.name,
                    "pid": handle.process.pid,
                    "exit_code": handle.exit_code if handle.exit_code is not None else handle.process.poll(),
                    "graceful": handle.graceful,
                    "forced": handle.forced,
                    "stdout_log": str(handle.stdout_path),
                    "stderr_log": str(handle.stderr_path),
                }
            )
        return items

