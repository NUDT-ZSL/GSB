from __future__ import annotations

import contextlib
import json
import threading
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from elastic_control.models import canonical_json
from elastic_control.test_server import LocalControlServer, create_server

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


@contextlib.contextmanager
def running_server(
    fault_config: dict[str, Any] | None = None,
) -> Iterator[tuple[LocalControlServer, str]]:
    server = create_server("127.0.0.1", 0, fault_config or {})
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        yield server, f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def reset_server(url: str) -> None:
    request = urllib.request.Request(
        f"{url}/admin/reset",
        data=b"{}",
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=2) as response:
        response.read()


def server_state(url: str) -> dict[str, Any]:
    with urllib.request.urlopen(f"{url}/admin/state", timeout=2) as response:
        return json.loads(response.read().decode("utf-8"))


def mini_inputs(tmp: Path, *, target_tick0: int = 2, target_tick1: int = 0) -> tuple[Path, Path, Path]:
    services = {
        "services": [
            {
                "name": "tiny",
                "model_version": "tiny-v1",
                "memory_mib_per_replica": 1000,
                "compute_units_per_replica": 10,
                "min_replicas": 0,
                "max_replicas": 3,
                "cold_start_ticks": 1,
                "batch_window_ms": 5,
                "tokens_per_tick_per_replica": 100,
                "max_concurrent_launches": 2,
            }
        ]
    }
    nodes = {
        "compute_units_capacity": 100,
        "nodes": [
            {
                "name": "node-0",
                "gpu_count": 1,
                "memory_mib_per_gpu": 4000,
                "max_instances_per_node": 4,
                "topology_domain": "domain-0",
            }
        ],
    }
    trace = {
        "trace_id": "mini",
        "windows": [
            {
                "tick": 0,
                "arrival_rate": target_tick0,
                "avg_sequence_length": 100,
            },
            {
                "tick": 1,
                "arrival_rate": target_tick1,
                "avg_sequence_length": 100,
            },
        ],
    }
    services_path = tmp / "services.json"
    nodes_path = tmp / "nodes.json"
    trace_path = tmp / "trace.json"
    write_json(services_path, services)
    write_json(nodes_path, nodes)
    write_json(trace_path, trace)
    return services_path, nodes_path, trace_path


def stable_dump(payload: Any) -> str:
    return canonical_json(payload)

