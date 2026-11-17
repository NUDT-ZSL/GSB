from __future__ import annotations

import copy
import json
import socket
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


def free_ports(count: int) -> list[int]:
    sockets = []
    ports = []
    try:
        for _ in range(count):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.bind(("127.0.0.1", 0))
            sockets.append(sock)
            ports.append(sock.getsockname()[1])
    finally:
        for sock in sockets:
            sock.close()
    return ports


def raw_config(name: str = "clusters.json") -> dict[str, Any]:
    with (DATA / name).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_config(path: Path, payload: dict[str, Any]) -> Path:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def config_payload(*, ports: Iterable[int] | None = None,
                   versions: dict[str, int] | None = None,
                   declaration_version: str | None = None) -> dict[str, Any]:
    payload = raw_config()
    if ports is not None:
        for cluster, port in zip(payload["clusters"], ports):
            cluster["port"] = int(port)
    if versions:
        for cluster in payload["clusters"]:
            requested = versions.get(cluster["cluster_id"])
            if requested is None or requested == cluster["config_version"]:
                continue
            history = list(cluster.get("version_history", [cluster["config_version"]]))
            history.append(requested)
            cluster["version_history"] = history
            cluster["config_version"] = requested
    if declaration_version is not None:
        payload["declaration_version"] = declaration_version
    return payload


def mutate_config(mutator) -> dict[str, Any]:
    payload = copy.deepcopy(raw_config())
    mutator(payload)
    return payload

