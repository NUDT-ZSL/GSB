"""Serve deterministic loopback-only health endpoints for the sample scene."""

from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from failover_console.scene import SceneLoader


class HealthHandler(BaseHTTPRequestHandler):
    server_version = "FailoverMock/1.0"

    def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
        if self.path.split("?", 1)[0] != "/health":
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"not found")
            return
        mode = getattr(self.server, "health_mode", "ok")
        if mode == "timeout":
            time.sleep(getattr(self.server, "slow_seconds", 2.0))
        status = 503 if mode == "error" else 200
        body = json.dumps(
            {
                "status": "ok" if status == 200 else "error",
                "instance_id": getattr(self.server, "instance_id", ""),
            }
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, fmt: str, *args: Any) -> None:
        return


def _parse_mode_map(values: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        instance_id, separator, mode = value.partition("=")
        if not separator or not instance_id or mode not in {"ok", "error", "timeout"}:
            raise argparse.ArgumentTypeError(
                f"无效模式 {value!r}，应为 INSTANCE=ok|error|timeout"
            )
        result[instance_id] = mode
    return result


def _parse_port_map(values: list[str]) -> dict[str, int]:
    result: dict[str, int] = {}
    for value in values:
        instance_id, separator, port_text = value.partition("=")
        try:
            port = int(port_text)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"无效端口映射 {value!r}") from exc
        if not separator or not instance_id or not 1 <= port <= 65535:
            raise argparse.ArgumentTypeError(f"无效端口映射 {value!r}")
        result[instance_id] = port
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="启动本地回环健康探测模拟实例")
    parser.add_argument("--scene", required=True, help="场景 JSON 路径")
    parser.add_argument(
        "--port-map",
        action="append",
        default=[],
        metavar="INSTANCE=PORT",
        help="覆盖实例回环端口，可重复",
    )
    parser.add_argument(
        "--mode",
        action="append",
        default=[],
        metavar="INSTANCE=ok|error|timeout",
        help="覆盖实例 HTTP 响应模式，可重复",
    )
    parser.add_argument("--ready-file", help="写入就绪 JSON 的路径")
    args = parser.parse_args(argv)

    scene = SceneLoader().load(args.scene)
    try:
        port_map = _parse_port_map(args.port_map)
        scene = scene.with_port_overrides(port_map)
        mode_map = _parse_mode_map(args.mode)
    except (KeyError, ValueError, argparse.ArgumentTypeError) as exc:
        print(json.dumps({"code": "E_SCENE", "message": str(exc)}), file=sys.stderr)
        return 2

    servers: list[ThreadingHTTPServer] = []
    threads: list[threading.Thread] = []
    try:
        for instance in scene.instances():
            server = ThreadingHTTPServer(("127.0.0.1", instance.port), HealthHandler)
            server.daemon_threads = True
            server.instance_id = instance.instance_id  # type: ignore[attr-defined]
            mode = mode_map.get(instance.instance_id, "ok")
            server.health_mode = mode  # type: ignore[attr-defined]
            server.slow_seconds = instance.probe.timeout_ms / 1000.0 + 0.5  # type: ignore[attr-defined]
            thread = threading.Thread(
                target=server.serve_forever,
                name=f"mock-{instance.instance_id}",
                daemon=True,
            )
            thread.start()
            servers.append(server)
            threads.append(thread)
    except OSError as exc:
        for server in servers:
            server.shutdown()
            server.server_close()
        print(
            json.dumps(
                {
                    "code": "E_SCENE",
                    "message": f"无法绑定回环端口，请用 --port-map 改写: {exc}",
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2

    ready = {
        "ready": True,
        "scene_version": scene.scene_version,
        "instances": [
            {"instance_id": item.instance_id, "port": item.port, "mode": mode_map.get(item.instance_id, "ok")}
            for item in scene.instances()
        ],
    }
    if args.ready_file:
        Path(args.ready_file).write_text(
            json.dumps(ready, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    print(json.dumps(ready, ensure_ascii=False), flush=True)

    stop = threading.Event()

    def request_stop(signum: int, frame: Any) -> None:
        stop.set()

    signal.signal(signal.SIGINT, request_stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, request_stop)
    try:
        while not stop.wait(0.25):
            pass
    finally:
        for server in servers:
            server.shutdown()
        for server in servers:
            server.server_close()
        for thread in threads:
            thread.join(timeout=1.0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
