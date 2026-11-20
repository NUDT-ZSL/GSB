from __future__ import annotations

import time

from deploy_convergence.canonical import canonical_json


def test_cross_environment_divergence_and_dependency_derivation(
    engine_factory,
    clock_ms: int,
) -> None:
    result = engine_factory().run(clock_ms=clock_ms)
    view = result.view
    consistency = view["consistency"]
    first = consistency["first_divergence"]
    assert first["service"] == "payments-api"
    assert first["parameter"] == "retry_factor"
    assert first["involved_environments"] == ["production"]
    assert first["max_difference"] == "0.25"
    assert first["tolerance"] == "0.05"
    assert first["excess"] == "0.2"
    assert first["values"] == {
        "development": 1.1,
        "staging": 1.1,
        "production": 1.35,
    }
    assert consistency["consistent"] is False

    gateway = view["derivations"]["development"]["gateway"]
    assert gateway["timeout_ms"] == 2450
    assert gateway["resource_limit"] == {
        "cpu_millicores": 250,
        "memory_mb": 256,
    }
    assert gateway["worst_upstream"] == "payments-api"
    assert gateway["dependency_chain"] == [
        "gateway",
        "payments-api",
        "ledger-api",
        "auth-service",
    ]

    states = {
        item["service"]: item["status"]
        for item in consistency["results"]
    }
    assert states["legacy-worker"] == "tombstoned"
    assert states["retired-cache"] == "expired"

    first_copy = canonical_json(view)
    second_copy = canonical_json(view)
    assert first_copy == second_copy


def test_engine_does_not_read_system_wall_clock(
    engine_factory,
    clock_ms: int,
    monkeypatch,
) -> None:
    def forbidden() -> float:
        raise AssertionError("wall clock must not be read")

    monkeypatch.setattr(time, "time", forbidden)
    monkeypatch.setattr(time, "time_ns", forbidden)
    result = engine_factory(state_name="no-wall-clock.sqlite").run(
        clock_ms=clock_ms
    )
    assert result.ok is True
    assert result.view["compatibility"]["wall_clock_read"] is False
