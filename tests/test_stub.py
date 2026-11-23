from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from scaler.launch_client import LaunchClient
from scaler.stub import StubServer


def test_inflight_duplicate_launch_waits_and_reuses_same_result() -> None:
    scenario = {
        "services": {
            "svc": {
                "mode": "fail_first_n",
                "failures": 1,
                "delay_ms": 100,
                "then": {"mode": "success", "delay_ms": 100},
            }
        }
    }
    with StubServer(port=0, scenario=scenario) as stub:
        client = LaunchClient(stub.url)

        def call():
            return client.launch(
                operation_id="same-operation",
                service="svc",
                replica_id="svc-000001",
                model_version="v1",
                cold_start_budget_ms=1000,
                round_no=1,
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: call(), range(2)))
        assert all(result.ok is False for result in results)
        assert all(result.code == "E_LAUNCH_HTTP_ERROR" for result in results)
        assert stub.state.service_attempts["svc"] == 1
        assert len(stub.state.operations) == 1
        assert not stub.state.in_progress
