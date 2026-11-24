from __future__ import annotations

import socket
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from smugglebench.codes import (
    D_BOUNDARY,
    D_CHUNK_LENGTH,
    D_DUP_HEADER,
    D_TE_CL,
    D_TRAILER,
    E_BUDGET,
    E_ECHO_UNREACHABLE,
    E_PEER_CLOSED,
    E_TIMEOUT,
    E_UNEXPECTED_STATUS,
)
from smugglebench.engine import RunConfig, Workbench
from smugglebench.echo_server import EchoServer
from smugglebench.transport import send_and_observe


def _by_id(report: dict) -> dict[str, dict]:
    return {item["mutation_id"]: item for item in report["mutations"]}


def test_real_echo_divergences_delay_and_overturn(
    sample_workspace: Path,
    tmp_path: Path,
) -> None:
    report = Workbench(
        sample_workspace,
        tmp_path / "run",
        RunConfig(concurrency=2, resume=False),
    ).run()
    mutations = _by_id(report)
    assert D_TE_CL in mutations["m01-te-cl-conflict"]["discrepancies"]
    assert D_DUP_HEADER in mutations["m02-duplicate-content-length"]["discrepancies"]
    assert D_CHUNK_LENGTH in mutations["m03-chunk-length-distortion"]["discrepancies"]
    assert D_TRAILER in mutations["m04-trailer-injection"]["discrepancies"]
    assert D_BOUNDARY in mutations["m05-boundary-injection"]["discrepancies"]
    assert mutations["m08-delayed-clean"]["latency_ms"] >= 110
    assert mutations["m06-flag-control"]["overturn_count"] == 1
    assert mutations["m07-gap"]["error"]["code"] == "E_MSG_GAP"
    assert mutations["m10-mixed-connection"]["error"]["code"] == "E_MIX_CONN"
    assert report["connections"]["conn-dupdelivery"]["duplicates"][0]["duplicate_count"] == 1


def test_concurrency_matches_serial_deterministic_result(
    sample_workspace: Path,
    tmp_path: Path,
) -> None:
    serial = Workbench(
        sample_workspace,
        tmp_path / "serial",
        RunConfig(concurrency=1, resume=False, default_echo_delay_ms=5),
    ).run()
    parallel = Workbench(
        sample_workspace,
        tmp_path / "parallel",
        RunConfig(concurrency=4, resume=False, default_echo_delay_ms=5),
    ).run()
    assert (
        parallel["deterministic_summary"]
        == serial["deterministic_summary"]
    )
    assert all(
        item["delay_observation"]["clock"] == "monotonic_ns"
        for item in parallel["mutations"]
    )


def test_inflight_byte_budget(sample_workspace: Path, tmp_path: Path) -> None:
    report = Workbench(
        sample_workspace,
        tmp_path / "budget",
        RunConfig(concurrency=4, resume=False, max_inflight_bytes=8),
    ).run()
    values = _by_id(report)
    for mutation_id in ("m01-te-cl-conflict", "m02-duplicate-content-length"):
        assert values[mutation_id]["error"]["code"] == E_BUDGET
        assert values[mutation_id]["sent"] is False


def test_timeout_terminates_later_sends(
    sample_workspace: Path,
    tmp_path: Path,
) -> None:
    report = Workbench(
        sample_workspace,
        tmp_path / "timeout",
        RunConfig(
            concurrency=2,
            resume=False,
            connection_timeout_s=0.05,
            default_echo_delay_ms=500,
            echo_read_timeout_s=2.0,
        ),
    ).run()
    values = _by_id(report)
    assert values["m05-boundary-injection"]["error"]["code"] == E_TIMEOUT
    assert values["m08-delayed-clean"]["error"]["code"] == E_TIMEOUT
    assert values["m08-delayed-clean"]["sent"] is False


def test_peer_close_and_unexpected_status_are_isolated(
    sample_workspace: Path,
    tmp_path: Path,
) -> None:
    report = Workbench(
        sample_workspace,
        tmp_path / "faults",
        RunConfig(
            concurrency=2,
            resume=False,
            echo_faults={
                "m06-flag-control": {"close_early": True},
                "m09-duplicate-delivery": {"status": 503},
            },
        ),
    ).run()
    values = _by_id(report)
    assert values["m06-flag-control"]["error"]["code"] == E_PEER_CLOSED
    assert values["m09-duplicate-delivery"]["error"]["code"] == E_UNEXPECTED_STATUS
    assert values["m08-delayed-clean"]["status"] == "OK"


def test_unreachable_echo_does_not_abort_other_connections(
    sample_workspace: Path,
    tmp_path: Path,
) -> None:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    report = Workbench(
        sample_workspace,
        tmp_path / "unreachable",
        RunConfig(
            concurrency=3,
            resume=False,
            start_echo_server=False,
            echo_port=port,
        ),
    ).run()
    values = _by_id(report)
    assert values["m06-flag-control"]["error"]["code"] == E_ECHO_UNREACHABLE
    assert values["m08-delayed-clean"]["error"]["code"] == E_ECHO_UNREACHABLE
    assert values["m07-gap"]["error"]["code"] == "E_MSG_GAP"


def test_message_count_budget_stops_excess_mutations(
    sample_workspace: Path,
    tmp_path: Path,
) -> None:
    report = Workbench(
        sample_workspace,
        tmp_path / "message-budget",
        RunConfig(concurrency=2, resume=False, max_messages_per_connection=1),
    ).run()
    values = _by_id(report)
    assert values["m05-boundary-injection"]["status"] != "ERROR"
    assert values["m08-delayed-clean"]["error"]["code"] == E_BUDGET
    assert values["m08-delayed-clean"]["sent"] is False


def test_identical_payloads_keep_independent_delay_profiles() -> None:
    payload = b"GET /same HTTP/1.1\r\nHost: local\r\n\r\n"
    with EchoServer() as server:
        def observe(delay_ms: int):
            def register(source_port: int) -> None:
                server.register(
                    payload,
                    delay_ms=delay_ms,
                    source_port=source_port,
                )

            return send_and_observe(
                payload,
                host=server.host,
                port=server.port,
                deadline_ns=time.monotonic_ns() + 3_000_000_000,
                on_bound=register,
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            fast = executor.submit(observe, 20)
            slow = executor.submit(observe, 180)
            fast_result = fast.result()
            slow_result = slow.result()
    assert fast_result.error is None
    assert slow_result.error is None
    assert fast_result.latency_ms >= 15
    assert slow_result.latency_ms >= 170
    assert slow_result.latency_ms > fast_result.latency_ms
