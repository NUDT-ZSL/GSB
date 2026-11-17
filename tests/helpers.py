from __future__ import annotations

import json
import threading
import time
from typing import Any

from failover_console.models import InstanceSpec, ProbeConfig, Scene
from failover_console.probe import ERROR, ProbeObservation, SUCCESS, UNREACHABLE
from failover_console.scene import SceneLoader


def instance(
    instance_id: str,
    role: str,
    port: int,
    *,
    timeout_ms: int = 80,
    retries: int = 1,
    success_threshold: int = 1,
    failure_threshold: int = 1,
) -> dict[str, Any]:
    return {
        "instance_id": instance_id,
        "role": role,
        "port": port,
        "probe": {
            "timeout_ms": timeout_ms,
            "retries": retries,
            "success_threshold": success_threshold,
            "failure_threshold": failure_threshold,
        },
    }


def service(
    service_id: str,
    instances: list[dict[str, Any]],
    *,
    mode: str = "auto",
    cooldown_ms: int = 60_000,
    min_healthy_replicas: int = 1,
    policy_version: str = "p1",
) -> dict[str, Any]:
    return {
        "service_id": service_id,
        "policy": {
            "mode": mode,
            "cooldown_ms": cooldown_ms,
            "min_healthy_replicas": min_healthy_replicas,
            "version": policy_version,
        },
        "instances": instances,
    }


def scene_data(
    services: list[dict[str, Any]],
    *,
    faults: list[dict[str, Any]] | None = None,
    scene_version: str = "test-v1",
    global_concurrency: int = 4,
    service_quota: int = 2,
) -> dict[str, Any]:
    return {
        "scene_version": scene_version,
        "probe_limits": {
            "global_concurrency": global_concurrency,
            "service_quota": service_quota,
        },
        "services": services,
        "faults": faults or [],
    }


def load_scene(data: dict[str, Any]) -> Scene:
    return SceneLoader().loads(json.dumps(data, ensure_ascii=False, indent=2))


class SuccessTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[int, int]] = []
        self.lock = threading.Lock()

    def probe(self, port: int, timeout_ms: int) -> ProbeObservation:
        with self.lock:
            self.calls.append((port, timeout_ms))
        return ProbeObservation(SUCCESS, "ok")


class BlockingSuccessTransport:
    def __init__(self, delay_seconds: float = 0.04) -> None:
        self.delay_seconds = delay_seconds
        self.lock = threading.Lock()
        self.active = 0
        self.peak = 0

    def probe(self, port: int, timeout_ms: int) -> ProbeObservation:
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            time.sleep(self.delay_seconds)
            return ProbeObservation(SUCCESS, "ok")
        finally:
            with self.lock:
                self.active -= 1


class RetryThenSuccessTransport:
    def __init__(self) -> None:
        self.calls: dict[int, int] = {}
        self.lock = threading.Lock()

    def probe(self, port: int, timeout_ms: int) -> ProbeObservation:
        with self.lock:
            self.calls[port] = self.calls.get(port, 0) + 1
            attempt = self.calls[port]
        if attempt == 1:
            return ProbeObservation(UNREACHABLE, "first attempt fails")
        return ProbeObservation(SUCCESS, "second attempt succeeds")


class AlwaysErrorTransport:
    def probe(self, port: int, timeout_ms: int) -> ProbeObservation:
        return ProbeObservation(ERROR, "forced error")


def inst_specs(scene: Scene) -> list[InstanceSpec]:
    return list(scene.instances())


def probe_config(
    timeout_ms: int = 80,
    retries: int = 1,
    success_threshold: int = 1,
    failure_threshold: int = 1,
) -> ProbeConfig:
    return ProbeConfig(timeout_ms, retries, success_threshold, failure_threshold)
