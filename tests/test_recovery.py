from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys

from gateway.service import GatewayService

from .conftest import ROOT, SAMPLES, call_gateway, principals


def _tokens(state):
    return {
        (item["bucket_kind"], item["bucket_key"]): item["tokens_units"]
        for item in state["token_buckets"]
    }


def test_forced_termination_recovers_pending_without_double_charge(
    service, database_path, upstream_server
):
    p = principals()
    completed_before_crash = call_gateway(
        service,
        principal=p["api_a_v1"],
        request_id="crash-before",
        request_seq=1,
        logical_time_ms=9_000,
        path="/v1/down",
    )
    assert completed_before_crash.status == 502

    env = dict(os.environ)
    env["GATEWAY_FAULT_AFTER_RESERVATION"] = "1"
    crashed = subprocess.run(
        [
            sys.executable,
            "-m",
            "tests.multiprocess_worker",
            "--db",
            str(database_path),
            "--config",
            str(SAMPLES / "config_v1.json"),
            "--request-id",
            "crash-pending",
            "--request-seq",
            "2",
            "--logical-time-ms",
            "20000",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert crashed.returncode == 86, crashed.stderr

    with sqlite3.connect(database_path) as conn:
        row = conn.execute(
            "SELECT state, request_id FROM requests WHERE tenant = 'tenant-a' AND request_seq = 2"
        ).fetchone()
        assert row == ("pending", "crash-pending")

    pending_state = service.state_snapshot("tenant-a")
    pending_tokens = _tokens(pending_state)
    assert pending_tokens[("route", "down")] == 2_000
    assert pending_tokens[("tenant", "tenant-a")] == 5_000
    attempts_before_recovery = sum(
        item["attempts"] for item in pending_state["window_buckets"]
    )
    assert attempts_before_recovery == 2

    resumed = GatewayService(
        database_path,
        initial_config_path=SAMPLES / "config_v1.json",
    )
    assert resumed.recovery_report["recovered_count"] == 1
    recovered_state = resumed.state_snapshot("tenant-a")
    assert recovered_state["request_counts"]["recovered"] == 1
    assert _tokens(recovered_state) == pending_tokens
    assert (
        sum(item["attempts"] for item in recovered_state["window_buckets"])
        == attempts_before_recovery
    )

    replay = call_gateway(
        resumed,
        principal=principals()["api_a_v2"],
        request_id="crash-pending",
        request_seq=2,
        logical_time_ms=20_000,
        path="/v1/down",
    )
    assert replay.status == 503
    assert replay.code == "E_RECOVERY_INCOMPLETE"
    assert _tokens(resumed.state_snapshot("tenant-a")) == pending_tokens

    next_request = call_gateway(
        resumed,
        principal=principals()["api_a_v2"],
        request_id="after-recovery",
        request_seq=3,
        logical_time_ms=20_001,
        path="/v1/down",
    )
    assert next_request.status == 502
    final_state = resumed.state_snapshot("tenant-a")
    final_tokens = _tokens(final_state)
    assert final_tokens[("route", "down")] == 1_001
    assert final_tokens[("tenant", "tenant-a")] == 4_001
    assert sum(item["attempts"] for item in final_state["window_buckets"]) == 3

    with sqlite3.connect(database_path) as conn:
        recovery_detail = conn.execute(
            "SELECT detail FROM recovery_events WHERE request_id = 'crash-pending'"
        ).fetchone()
        assert recovery_detail is not None
        assert "without duplicate" in recovery_detail[0]
