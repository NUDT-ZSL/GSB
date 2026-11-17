from __future__ import annotations

import json
import shutil
import socket
from pathlib import Path

from ml_gateway.canonical import canonical_json
from ml_gateway.declarations import load_declarations, load_requests_ndjson
from ml_gateway.engine import GatewayEngine
from ml_gateway.logger import StructuredLogger
from ml_gateway.services import (
    BackendPool,
    GatewayTCPServer,
    ensure_ports_available,
    send_gateway_requests,
)
from ml_gateway.storage import SessionStore

from tests.conftest import DATA_DIR


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def _live_bundle(tmp_path: Path):
    params = tmp_path / "params"
    params.mkdir(parents=True)
    for name in ("alpha.json", "beta.json"):
        shutil.copyfile(DATA_DIR / "params" / name, params / name)
    backends = json.loads((DATA_DIR / "backends.json").read_text(encoding="utf-8"))
    ports = [_free_port() for _ in backends["backends"]]
    for backend, port in zip(backends["backends"], ports):
        backend["port"] = port
    backends_path = tmp_path / "backends.json"
    rules_path = tmp_path / "rules.json"
    backends_path.write_text(json.dumps(backends) + "\n", encoding="utf-8")
    rules_path.write_text((DATA_DIR / "rules.json").read_text(encoding="utf-8"), encoding="utf-8")
    bundle = load_declarations(backends_path, rules_path)
    return bundle, ports


def test_gateway_and_backend_jsonl_tcp_roundtrip(tmp_path, capsys):
    bundle, backend_ports = _live_bundle(tmp_path)
    gateway_port = _free_port()
    ensure_ports_available("127.0.0.1", [gateway_port] + backend_ports)
    requests = load_requests_ndjson(DATA_DIR / "requests.ndjson", bundle.feature_schema)
    store = SessionStore(tmp_path / "session.db")
    logger = StructuredLogger(store, tmp_path / "gateway.jsonl", fresh=True)
    engine = GatewayEngine(store=store, bundle=bundle, logger=logger)
    pool = BackendPool.from_states(engine.states, bundle)
    gateway = GatewayTCPServer(host="127.0.0.1", port=gateway_port, engine=engine)
    pool.start()
    try:
        gateway.start()
        responses = send_gateway_requests(
            host="127.0.0.1", port=gateway_port, requests=requests
        )
    finally:
        gateway.stop()
        pool.stop()
    assert len(responses) == 8
    assert all(response["status"] == "OK" for response in responses)
    assert all(response["output_summary"].startswith("out1:") for response in responses)
    assert all(isinstance(response["duration_ms"], int) for response in responses)
    assert len([row for row in store.list_ledger() if row["effective"]]) == 8
    store.close()


def test_numpy_tcp_outputs_are_byte_identical_across_runs(tmp_path):
    bundle, backend_ports = _live_bundle(tmp_path)
    gateway_port = _free_port()
    requests = load_requests_ndjson(DATA_DIR / "requests.ndjson", bundle.feature_schema)
    outputs = []
    for run_number in range(2):
        ensure_ports_available("127.0.0.1", [gateway_port] + backend_ports)
        store = SessionStore(tmp_path / f"session-{run_number}.db")
        logger = StructuredLogger(
            store, tmp_path / f"gateway-{run_number}.jsonl", fresh=True
        )
        engine = GatewayEngine(store=store, bundle=bundle, logger=logger)
        pool = BackendPool.from_states(engine.states, bundle)
        gateway = GatewayTCPServer(host="127.0.0.1", port=gateway_port, engine=engine)
        pool.start()
        try:
            gateway.start()
            responses = send_gateway_requests(
                host="127.0.0.1", port=gateway_port, requests=requests
            )
        finally:
            gateway.stop()
            pool.stop()
        outputs.append(canonical_json(responses).encode("utf-8"))
        store.close()
    assert outputs[0] == outputs[1]


def test_unreachable_backend_records_e_upstream_and_reviews_bindings(tmp_path):
    bundle, backend_ports = _live_bundle(tmp_path)
    requests = load_requests_ndjson(DATA_DIR / "requests.ndjson", bundle.feature_schema)
    store = SessionStore(tmp_path / "session.db")
    logger = StructuredLogger(store, tmp_path / "gateway.jsonl", fresh=True)
    engine = GatewayEngine(store=store, bundle=bundle, logger=logger)
    pool = BackendPool.from_states(engine.states, bundle)
    pool.start()
    try:
        engine.execute_requests(requests)
        pool.services["be-alpha"].stop()
        report = engine.execute_requests(requests)
    finally:
        pool.stop()
    alpha_responses = [
        response for response in report.responses if response["backend_id"] == "be-alpha"
    ]
    assert alpha_responses
    assert all(response["status"] == "E_UPSTREAM" for response in alpha_responses)
    assert any(
        batch["error_code"] == "E_UPSTREAM" for batch in store.list_batches()
    )
    assert any(event["code"] == "BINDING_REVIEW" for event in store.list_log_events())
    store.close()
