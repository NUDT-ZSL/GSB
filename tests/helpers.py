from __future__ import annotations

import copy
import json
import socket
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

from lbdiag.model import parse_sample
from lbdiag.service import Handler, RouteState

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "samples" / "diagnostic_sample.json"


def sample_raw() -> dict[str, Any]:
    return json.loads(SAMPLE.read_text(encoding="utf-8"))


def config_for_port(port: int, refused_port: int, *, mutate=None):
    raw = sample_raw()
    raw["diagnostics"].pop("sample_sha256", None)
    raw["test_service"]["port"] = port
    raw["test_service"]["refused_port"] = refused_port
    for instance in raw["instances"]:
        if instance["id"] == "epsilon":
            instance["address"] = (
                f"http://127.0.0.1:{refused_port}/health/refused"
            )
        else:
            path = instance["address"].split("/", 3)[3]
            instance["address"] = f"http://127.0.0.1:{port}/{path}"
    if mutate:
        mutate(raw)
    return parse_sample(raw)


def write_sample(path: Path, raw: dict[str, Any]) -> Path:
    path.write_text(
        json.dumps(raw, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    return path


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class RunningServer:
    def __init__(self, routes: dict[str, Any], host: str = "127.0.0.1") -> None:
        self.server = ThreadingHTTPServer((host, 0), Handler)
        self.server.daemon_threads = True
        self.server.route_state = RouteState(routes)  # type: ignore[attr-defined]
        self.port = int(self.server.server_address[1])
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "RunningServer":
        self.thread.start()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def clone(value: Any) -> Any:
    return copy.deepcopy(value)

