from __future__ import annotations

import copy
import json
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from lbhealth.config import Config, parse_config
from lbhealth.models import ProbeResult

ROOT = Path(__file__).resolve().parents[1]


def base_raw() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "config_version": "test-v1",
        "pool": {
            "probe_protocol": "http",
            "interval_ms": 500,
            "timeout_ms": 100,
            "success_threshold": 2,
            "failure_threshold": 2,
            "slow_start_weight_table": [25, 50, 75, 100],
            "members": [
                {
                    "id": "alpha",
                    "address": "127.0.0.1",
                    "port": 19001,
                    "protocol": "http",
                    "weight": 60,
                    "probe_path": "/health",
                },
                {
                    "id": "bravo",
                    "address": "127.0.0.1",
                    "port": 19002,
                    "protocol": "tcp",
                    "weight": 40,
                },
            ],
        },
    }


def make_config(mutator: Any | None = None) -> Config:
    raw = base_raw()
    if mutator is not None:
        mutator(raw)
    return parse_config(raw)


def probe(round_no: int, member_id: str, ok: bool, protocol: str = "http") -> ProbeResult:
    now = time.monotonic_ns() // 1_000_000
    return ProbeResult(
        round_no=round_no,
        member_id=member_id,
        protocol=protocol,
        ok=ok,
        start_mono_ms=now,
        end_mono_ms=now + (2 if ok else 100),
        summary="HTTP 200; ok" if ok else "socket timeout",
        failure_code=None if ok else "timeout",
    )


class PlanProber:
    """Deterministic fake prober keyed by round number."""

    def __init__(self, config: Config, plan: dict[int, dict[str, bool]]) -> None:
        self.config = config
        self.plan = copy.deepcopy(plan)
        self.calls: list[int] = []

    def probe_all(self, round_no: int) -> list[ProbeResult]:
        self.calls.append(round_no)
        outcomes = self.plan[round_no]
        return [
            probe(round_no, member.id, bool(outcomes[member.id]), member.protocol)
            for member in self.config.members
        ]


class SequenceProber:
    def __init__(self, config: Config, rounds: list[dict[str, bool]]) -> None:
        self.config = config
        self.rounds = copy.deepcopy(rounds)
        self.index = 0

    def probe_all(self, round_no: int) -> list[ProbeResult]:
        outcomes = self.rounds[self.index]
        self.index += 1
        return [
            probe(round_no, member.id, bool(outcomes[member.id]), member.protocol)
            for member in self.config.members
        ]


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class BackendProcess:
    def __init__(
        self,
        temp_dir: Path,
        protocol: str,
        behavior: dict[str, Any],
        start_index: int = 0,
    ) -> None:
        self.temp_dir = temp_dir
        self.protocol = protocol
        self.port = free_port()
        self.behavior_path = temp_dir / f"behavior-{protocol}-{self.port}.json"
        self.ready_path = temp_dir / f"ready-{protocol}-{self.port}.json"
        self.state_path = temp_dir / f"state-{protocol}-{self.port}.json"
        self.behavior_path.write_text(json.dumps(behavior) + "\n", encoding="utf-8")
        self.start_index = start_index
        self.process: subprocess.Popen[bytes] | None = None

    def __enter__(self) -> "BackendProcess":
        command = [
            sys.executable,
            str(ROOT / "scripts" / "backend_sim.py"),
            "--protocol",
            self.protocol,
            "--port",
            str(self.port),
            "--behavior",
            str(self.behavior_path),
            "--start-index",
            str(self.start_index),
            "--state-file",
            str(self.state_path),
            "--ready-file",
            str(self.ready_path),
        ]
        self.process = subprocess.Popen(
            command,
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.ready_path.exists():
                return self
            if self.process.poll() is not None:
                stdout, stderr = self.process.communicate()
                raise RuntimeError(
                    f"backend exited: {self.process.returncode} {stdout!r} {stderr!r}"
                )
            time.sleep(0.02)
        raise RuntimeError("backend did not become ready")

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=5)
        if self.process is not None:
            if self.process.stdout is not None:
                self.process.stdout.close()
            if self.process.stderr is not None:
                self.process.stderr.close()
