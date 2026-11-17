"""Subprocess lifecycle management for cluster simulation services."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .config import ClusterSpec
from .errors import E_PORT, BusinessError
from .logging_utils import EventLogger


def check_loopback_port_available(cluster: ClusterSpec) -> None:
    family = socket.AF_INET6 if cluster.host == "::1" else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        if os.name != "nt":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((cluster.host, cluster.port))
    except OSError as exc:
        raise BusinessError(
            E_PORT,
            f"loopback port is already occupied for cluster {cluster.cluster_id}",
            details={
                "cluster_id": cluster.cluster_id,
                "host": cluster.host,
                "port": cluster.port,
                "os_error": str(exc),
            },
        ) from exc
    finally:
        sock.close()


@dataclass
class ManagedService:
    cluster: ClusterSpec
    process: subprocess.Popen[bytes]


class ClusterServiceManager:
    """Starts services sequentially and stops all children on exit."""

    def __init__(self, config_path: str, sample_path: str, log_path: str | None = None,
                 startup_timeout_s: float = 5.0) -> None:
        self.config_path = str(Path(config_path).resolve())
        self.sample_path = str(Path(sample_path).resolve())
        self.log_path = str(Path(log_path).resolve()) if log_path else None
        self.startup_timeout_s = startup_timeout_s
        self.services: list[ManagedService] = []
        self.logger = EventLogger(log_path)
        self.project_root = Path(__file__).resolve().parent.parent

    def _probe(self, cluster: ClusterSpec) -> bool:
        family = socket.AF_INET6 if cluster.host == "::1" else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.settimeout(0.15)
        try:
            sock.connect((cluster.host, cluster.port))
            return True
        except OSError:
            return False
        finally:
            sock.close()

    def start_cluster(self, cluster: ClusterSpec) -> ManagedService:
        check_loopback_port_available(cluster)
        command = [
            sys.executable,
            "-m",
            "cluster_ledger.server",
            "--config",
            self.config_path,
            "--sample-data",
            self.sample_path,
            "--cluster-id",
            cluster.cluster_id,
        ]
        if self.log_path:
            command.extend(["--log", self.log_path])
        env = os.environ.copy()
        existing = env.get("PYTHONPATH")
        env["PYTHONPATH"] = str(self.project_root) + (os.pathsep + existing if existing else "")
        process = subprocess.Popen(
            command,
            cwd=str(self.project_root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        deadline = time.monotonic() + self.startup_timeout_s
        while time.monotonic() < deadline:
            if process.poll() is not None:
                stderr = b""
                if process.stderr is not None:
                    stderr = process.stderr.read()
                if process.stdout is not None:
                    process.stdout.close()
                if process.stderr is not None:
                    process.stderr.close()
                text = stderr.decode("utf-8", errors="replace").strip()
                raise BusinessError(
                    E_PORT,
                    f"cluster service {cluster.cluster_id} exited before accepting connections",
                    details={
                        "cluster_id": cluster.cluster_id,
                        "host": cluster.host,
                        "port": cluster.port,
                        "exit_code": process.returncode,
                        "service_output": text[-4000:],
                    },
                )
            if self._probe(cluster):
                service = ManagedService(cluster=cluster, process=process)
                self.services.append(service)
                self.logger.emit(
                    "service_process_started",
                    cluster_id=cluster.cluster_id,
                    pid=process.pid,
                    host=cluster.host,
                    port=cluster.port,
                    config_version=cluster.config_version,
                )
                return service
            time.sleep(0.03)
        self._terminate(process)
        raise BusinessError(
            E_PORT,
            f"cluster service {cluster.cluster_id} did not become ready",
            details={"cluster_id": cluster.cluster_id, "host": cluster.host, "port": cluster.port},
        )

    def start_many(self, clusters: Iterable[ClusterSpec]) -> list[ManagedService]:
        try:
            for cluster in clusters:
                self.start_cluster(cluster)
        except Exception:
            self.stop_all()
            raise
        return list(self.services)

    @staticmethod
    def _terminate(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2.0)
        if process.stdout is not None and not process.stdout.closed:
            process.stdout.close()
        if process.stderr is not None and not process.stderr.closed:
            process.stderr.close()

    def stop_all(self) -> None:
        for service in reversed(self.services):
            self._terminate(service.process)
            self.logger.emit(
                "service_process_stopped",
                cluster_id=service.cluster.cluster_id,
                pid=service.process.pid,
                exit_code=service.process.returncode,
            )
        self.services.clear()

    def __enter__(self) -> "ClusterServiceManager":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop_all()
