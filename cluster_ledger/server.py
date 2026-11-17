"""Deterministic localhost cluster simulation service."""

from __future__ import annotations

import argparse
import json
import os
import socket
import socketserver
import sys
import traceback
from pathlib import Path
from typing import Any

from .clock import SERVER_CLOCK_BASIS, mono_ms, mono_ns
from .config import ClusterSpec, Declaration, load_declaration, load_sample_data
from .deterministic import AggregationDataError, SUPPORTED_AGGREGATIONS, compute_aggregate
from .errors import E_CONFIG, E_SERVICE, E_VERSION, BusinessError
from .logging_utils import EventLogger
from .protocol import MAX_LINE_BYTES, PROTOCOL_VERSION, decode_message, encode_message, error_response


def _int_field(value: Any, field_path: str, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise BusinessError(E_CONFIG, f"must be an integer >= {minimum}", field_path=field_path)
    return value


def _str_field(value: Any, field_path: str) -> str:
    if not isinstance(value, str) or not value:
        raise BusinessError(E_CONFIG, "must be a non-empty string", field_path=field_path)
    return value


def handle_dispatch(
    request: dict[str, Any], *, cluster: ClusterSpec, sample_data: dict[str, Any]
) -> dict[str, Any]:
    request_id = request.get("request_id")
    task_id = _str_field(request.get("task_id"), "$.task_id")
    cluster_id = _str_field(request.get("cluster_id"), "$.cluster_id")
    if cluster_id != cluster.cluster_id:
        raise BusinessError(
            E_VERSION,
            "request cluster is not served by this process",
            field_path="$.cluster_id",
            details={"expected": cluster.cluster_id, "actual": cluster_id},
        )
    config_version = _int_field(request.get("config_version"), "$.config_version", minimum=1)
    if config_version != cluster.config_version:
        raise BusinessError(
            E_VERSION,
            "request config version does not match the served cluster declaration",
            field_path="$.config_version",
            details={
                "cluster_id": cluster.cluster_id,
                "expected": cluster.config_version,
                "actual": config_version,
            },
        )
    config_keys = request.get("config_keys")
    if not isinstance(config_keys, list) or not config_keys:
        raise BusinessError(E_CONFIG, "must be a non-empty string array", field_path="$.config_keys")
    if any(not isinstance(key, str) or not key for key in config_keys):
        raise BusinessError(E_CONFIG, "must contain non-empty strings", field_path="$.config_keys")
    if len(set(config_keys)) != len(config_keys):
        raise BusinessError(E_CONFIG, "must not contain duplicates", field_path="$.config_keys")
    aggregation = request.get("aggregation")
    if not isinstance(aggregation, dict) or aggregation.get("mode") not in SUPPORTED_AGGREGATIONS:
        raise BusinessError(
            E_CONFIG,
            "aggregation.mode must be one of sha256, sum, count",
            field_path="$.aggregation.mode",
        )
    nodes = request.get("nodes")
    if nodes is not None and nodes != list(cluster.nodes):
        raise BusinessError(
            E_VERSION,
            "request node set does not match the served cluster declaration",
            field_path="$.nodes",
            details={"expected": list(cluster.nodes), "actual": nodes},
        )

    server_started = mono_ms()
    calc_started_ns = mono_ns()
    try:
        result = compute_aggregate(
            cluster_id=cluster.cluster_id,
            nodes=cluster.nodes,
            config_version=config_version,
            config_keys=config_keys,
            aggregation=aggregation,
            sample_data=sample_data,
        )
    except AggregationDataError as exc:
        raise BusinessError(E_CONFIG, str(exc), field_path=exc.field_path) from exc
    calculation_duration_ms = (mono_ns() - calc_started_ns) // 1_000_000
    server_finished = mono_ms()
    return {
        "ok": True,
        "protocol_version": PROTOCOL_VERSION,
        "request_id": request_id,
        "task_id": task_id,
        "cluster_id": cluster.cluster_id,
        "config_version": config_version,
        "aggregation_mode": result.aggregation_mode,
        "summary": result.summary,
        "components": result.components,
        "entry_count": result.entry_count,
        "calculation_duration_ms": calculation_duration_ms,
        "server_started_mono_ms": server_started,
        "server_finished_mono_ms": server_finished,
        "server_clock_basis": SERVER_CLOCK_BASIS,
    }


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        try:
            raw = self.rfile.readline(MAX_LINE_BYTES + 1)
        except OSError:
            return
        if not raw:
            return
        if len(raw) > MAX_LINE_BYTES:
            response = error_response(E_CONFIG, "request exceeds maximum line length", field_path="$")
        else:
            request_id = None
            try:
                request = decode_message(raw)
                request_id = request.get("request_id") if isinstance(request.get("request_id"), str) else None
                if request.get("type") != "dispatch":
                    raise BusinessError(
                        E_CONFIG, "type must be 'dispatch'", field_path="$.type"
                    )
                if request.get("protocol_version") not in (None, PROTOCOL_VERSION):
                    raise BusinessError(
                        E_CONFIG,
                        f"unsupported protocol_version; expected {PROTOCOL_VERSION}",
                        field_path="$.protocol_version",
                    )
                # These fields are echoed into the service log, but do not alter
                # deterministic aggregation.
                _int_field(request.get("timeout_ms", 1), "$.timeout_ms", minimum=1)
                _int_field(request.get("retries", 0), "$.retries", minimum=0)
                response = handle_dispatch(
                    request,
                    cluster=self.server.cluster,  # type: ignore[attr-defined]
                    sample_data=self.server.sample_data,  # type: ignore[attr-defined]
                )
                logger = self.server.logger  # type: ignore[attr-defined]
                logger.emit(
                    "service_dispatch",
                    request_id=request_id,
                    task_id=request.get("task_id"),
                    cluster_id=self.server.cluster.cluster_id,  # type: ignore[attr-defined]
                    config_version=request.get("config_version"),
                    summary=response.get("summary"),
                    calculation_duration_ms=response.get("calculation_duration_ms"),
                )
            except BusinessError as exc:
                response = error_response(
                    exc.code,
                    exc.message,
                    field_path=exc.field_path,
                    details=exc.details,
                    request_id=request_id,
                )
                self.server.logger.error(exc, event="service_rejection", request_id=request_id)  # type: ignore[attr-defined]
            except (ValueError, UnicodeDecodeError) as exc:
                response = error_response(
                    E_CONFIG,
                    f"malformed JSON line: {exc}",
                    field_path="$",
                    request_id=request_id,
                )
            except Exception as exc:  # pragma: no cover - external diagnostic path
                response = error_response(E_SERVICE, f"unexpected service error: {exc}", request_id=request_id)
                self.server.logger.emit(  # type: ignore[attr-defined]
                    "service_exception",
                    code=E_SERVICE,
                    traceback=traceback.format_exc(),
                )
        try:
            self.wfile.write(encode_message(response))
            self.wfile.flush()
        except OSError:
            pass
        # The client half-closes after sending its request. Consume that FIN so
        # the accepted service socket closes passively and does not occupy the
        # listening port with TIME_WAIT.
        try:
            self.request.settimeout(0.5)
            self.request.recv(1)
        except OSError:
            pass


def make_server(cluster: ClusterSpec, sample_data: dict[str, Any], logger: EventLogger):
    family = socket.AF_INET6 if cluster.host == "::1" else socket.AF_INET

    class Server(socketserver.ThreadingTCPServer):
        address_family = family
        # On Windows SO_REUSEADDR can allow two live listeners to bind the
        # same port, so keep exclusive semantics there.
        allow_reuse_address = os.name != "nt"
        daemon_threads = True
        request_queue_size = 64

    server = Server((cluster.host, cluster.port), _Handler)
    server.cluster = cluster  # type: ignore[attr-defined]
    server.sample_data = sample_data  # type: ignore[attr-defined]
    server.logger = logger  # type: ignore[attr-defined]
    return server


def run_service(config_path: str, sample_path: str, cluster_id: str,
                log_path: str | None = None) -> None:
    declaration = load_declaration(config_path)
    sample_data = load_sample_data(sample_path)
    try:
        cluster = declaration.cluster_map()[cluster_id]
    except KeyError as exc:
        raise BusinessError(
            E_CONFIG, f"cluster {cluster_id!r} is not declared", field_path="$.clusters"
        ) from exc
    logger = EventLogger(log_path)
    try:
        server = make_server(cluster, sample_data, logger)
    except OSError as exc:
        raise BusinessError(
            E_PORT,
            f"cannot bind loopback port {cluster.host}:{cluster.port}: {exc}",
            details={"cluster_id": cluster.cluster_id, "host": cluster.host, "port": cluster.port},
        ) from exc
    logger.emit(
        "service_started",
        cluster_id=cluster.cluster_id,
        host=cluster.host,
        port=cluster.port,
        config_version=cluster.config_version,
        clock_basis=SERVER_CLOCK_BASIS,
    )
    try:
        server.serve_forever(poll_interval=0.1)
    finally:
        server.server_close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one deterministic loopback cluster service")
    parser.add_argument("--config", required=True)
    parser.add_argument("--sample-data", required=True)
    parser.add_argument("--cluster-id", required=True)
    parser.add_argument("--log")
    args = parser.parse_args(argv)
    logger = EventLogger(args.log)
    try:
        run_service(args.config, args.sample_data, args.cluster_id, args.log)
        return 0
    except BusinessError as exc:
        logger.error(exc, event="service_start_failed")
        print(json.dumps(exc.to_dict(), ensure_ascii=False, sort_keys=True), file=sys.stderr, flush=True)
        return 2
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
