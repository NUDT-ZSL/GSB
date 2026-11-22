"""Deterministic loopback HTTP report simulator for tests and demos."""

from __future__ import annotations

import copy
import json
import socket
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Self
from urllib.parse import parse_qs, urlparse

from .canon import read_json, write_json
from .validation import validate_expected


def _mode_spec(value: Any) -> dict[str, Any]:
    if value is None:
        return {"mode": "match"}
    if isinstance(value, str):
        return {"mode": value}
    if isinstance(value, dict):
        result = dict(value)
        result.setdefault("mode", "match")
        return result
    raise ValueError("anomaly mode must be a string or object")


def build_report(
    expected_service: dict[str, Any],
    expected_instance: dict[str, Any],
    expected_clock_ms: int,
    spec: dict[str, Any],
) -> Any:
    """Build the JSON (or raw string) returned by one simulated instance."""

    mode = spec["mode"]
    if mode in ("malformed", "invalid_json"):
        return spec.get("raw", "{not-json")
    if mode == "non_object":
        return spec.get("raw", "[]")
    if mode == "empty":
        return ""
    report: dict[str, Any] = {
        "service": expected_service["service"],
        "instance_id": expected_instance["instance_id"],
        "version": expected_instance["version"],
        "digest": expected_instance["digest"],
        "logical_clock_ms": expected_clock_ms,
        "healthy": True,
        "rules": copy.deepcopy(expected_service["routes"]),
    }

    if mode in ("stale", "version_behind"):
        report["version"] = max(
            0, expected_instance["version"] + int(spec.get("version_offset", -1))
        )
        report["logical_clock_ms"] = max(
            0, expected_clock_ms + int(spec.get("clock_offset_ms", -100))
        )
    elif mode == "version_regression":
        report["version"] = int(spec.get("version", 0))
    elif mode == "tampered_digest":
        report["digest"] = spec.get(
            "digest", f"sha256:tampered-{expected_instance['instance_id']}"
        )
    elif mode == "missing_metadata":
        for field in spec.get("fields", ["digest", "logical_clock_ms"]):
            report.pop(field, None)
    elif mode == "missing_rule":
        target = spec.get("rule_id", expected_service["routes"][0]["rule_id"])
        report["rules"] = [
            rule for rule in report["rules"] if rule["rule_id"] != target
        ]
    elif mode == "extra_rule":
        report["rules"].append(
            {
                "rule_id": spec.get(
                    "rule_id", f"extra-{expected_instance['instance_id']}"
                ),
                "path": spec.get("path", "/undeclared"),
                "version_label": spec.get("version_label", "v-extra"),
                "weight": int(spec.get("weight", 0)),
            }
        )
    elif mode == "weight_mismatch":
        target = spec.get("rule_id", expected_service["routes"][0]["rule_id"])
        delta = int(spec.get("delta", 1))
        for rule in report["rules"]:
            if rule["rule_id"] == target:
                rule["weight"] = max(0, rule["weight"] + delta)
                break
    elif mode == "route_content_mismatch":
        target = spec.get("rule_id", expected_service["routes"][0]["rule_id"])
        for rule in report["rules"]:
            if rule["rule_id"] == target:
                rule[spec.get("field", "path")] = spec.get("value", "/changed")
                break
    elif mode == "unhealthy":
        report["healthy"] = False
    elif mode == "bad_field_type":
        report[spec.get("field", "version")] = spec.get("value", "not-an-integer")
    elif mode == "duplicate_rule":
        report["rules"].append(copy.deepcopy(report["rules"][0]))
    elif mode == "match":
        pass
    else:
        raise ValueError(f"unknown mock mode: {mode}")

    for field in spec.get("remove_fields", []):
        report.pop(field, None)
    for field, value in spec.get("set_fields", {}).items():
        report[field] = value
    remove_rules = set(spec.get("remove_rules", []))
    if remove_rules:
        report["rules"] = [
            rule for rule in report["rules"] if rule["rule_id"] not in remove_rules
        ]
    for extra_rule in spec.get("add_rules", []):
        report["rules"].append(copy.deepcopy(extra_rule))
    for rule in report["rules"]:
        overrides = spec.get("rule_overrides", {}).get(rule["rule_id"], {})
        for field, value in overrides.items():
            rule[field] = value
    return report


class _ReportHandler(BaseHTTPRequestHandler):
    server_version = "MeshDriftMock/1.0"

    def do_GET(self) -> None:
        cluster: MockCluster = self.server.cluster  # type: ignore[attr-defined]
        parsed = urlparse(self.path)
        instance_id = self.server.paths.get(parsed.path)  # type: ignore[attr-defined]
        if instance_id is None:
            self._send(HTTPStatus.NOT_FOUND, b'{"error":"unknown report path"}')
            return
        query = parse_qs(parsed.query)
        round_value = query.get("round", ["0"])[0]
        spec = cluster.spec_for(instance_id, round_value)
        delay_ms = int(spec.get("delay_ms", 0))
        if delay_ms > 0:
            time.sleep(delay_ms / 1000.0)
        if spec["mode"] == "http_error":
            status = int(spec.get("status", 500))
            body = spec.get("body", '{"error":"injected upstream failure"}')
            self._send(status, body.encode("utf-8"))
            return
        if spec["mode"] == "connection_reset":
            self.close_connection = True
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.connection.close()
            return
        payload = build_report(
            cluster.service_by_instance[instance_id],
            cluster.instance_by_id[instance_id],
            cluster.expected_clock_ms,
            spec,
        )
        if isinstance(payload, str):
            body = payload.encode("utf-8")
        else:
            body = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        self._send(200, body)

    def _send(self, status: int, body: bytes) -> None:
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        return


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class MockCluster:
    """One loopback HTTP listener per declared endpoint port."""

    def __init__(
        self,
        expected_snapshot: dict[str, Any],
        scenario: dict[str, Any] | None = None,
    ) -> None:
        self.expected = validate_expected(expected_snapshot)
        self.scenario = scenario or {"default": "match"}
        self.expected_clock_ms = self.expected["logical_clock_ms"]
        self.expected = self.expected
        self.instance_by_id: dict[str, dict[str, Any]] = {}
        self.service_by_instance: dict[str, dict[str, Any]] = {}
        self.path_by_port: dict[int, dict[str, str]] = {}
        endpoint_groups: dict[tuple[str, int], list[tuple[str, str]]] = {}
        for service in self.expected["services"]:
            for instance in service["instances"]:
                instance_id = instance["instance_id"]
                self.instance_by_id[instance_id] = instance
                self.service_by_instance[instance_id] = service
                parsed = urlparse(instance["endpoint"])
                if parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
                    raise ValueError(
                        f"mock endpoints must use a loopback host: {instance['endpoint']}"
                    )
                port = parsed.port
                if port is None:
                    raise ValueError("mock endpoint must include an explicit port")
                endpoint_groups.setdefault((parsed.hostname, port), []).append(
                    (parsed.path or "/", instance_id)
                )
                port_paths = self.path_by_port.setdefault(port, {})
                previous = port_paths.get(parsed.path or "/")
                if previous is not None and previous != instance_id:
                    raise ValueError(
                        f"report path collision on {parsed.path}: {previous}, {instance_id}"
                    )
                port_paths[parsed.path or "/"] = instance_id
        self._groups = endpoint_groups
        self._servers: list[_Server] = []
        self._threads: list[threading.Thread] = []

    def spec_for(self, instance_id: str, round_value: str) -> dict[str, Any]:
        rounds = self.scenario.get("rounds", {})
        by_instance = rounds.get(str(round_value), {})
        if instance_id in by_instance:
            return _mode_spec(by_instance[instance_id])
        instance_config = self.scenario.get("instances", {}).get(instance_id, {})
        if "by_round" in instance_config:
            by_round = instance_config["by_round"]
            if str(round_value) in by_round:
                return _mode_spec(by_round[str(round_value)])
        if "default" in instance_config:
            return _mode_spec(instance_config["default"])
        return _mode_spec(self.scenario.get("default", "match"))

    def start(self) -> MockCluster:
        for (host, port), paths in sorted(self._groups.items()):
            bind_host = "127.0.0.1" if host in ("localhost", "127.0.0.1") else host
            server = _Server((bind_host, port), _ReportHandler)
            server.cluster = self  # type: ignore[attr-defined]
            server.paths = self.path_by_port[port]  # type: ignore[attr-defined]
            thread = threading.Thread(
                target=server.serve_forever,
                name=f"mock-report-{port}",
                daemon=True,
            )
            thread.start()
            self._servers.append(server)
            self._threads.append(thread)
        return self

    def stop(self) -> None:
        for server in self._servers:
            server.shutdown()
            server.server_close()
        for thread in self._threads:
            thread.join(timeout=2.0)
        self._servers.clear()
        self._threads.clear()

    def __enter__(self) -> Self:
        return self.start()

    def __exit__(self, *exc_info: object) -> None:
        self.stop()


def main(
    expected_path: str,
    scenario_path: str,
    endpoints_output: str | None = None,
) -> int:
    expected = read_json(expected_path)
    scenario = read_json(scenario_path)
    cluster = MockCluster(expected, scenario).start()
    endpoints = {
        service["service"]: [instance["endpoint"] for instance in service["instances"]]
        for service in cluster.expected["services"]
    }
    print(json.dumps({"ok": True, "listening": endpoints}, ensure_ascii=False, sort_keys=True))
    if endpoints_output:
        write_json(endpoints_output, endpoints)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        cluster.stop()
    return 0
