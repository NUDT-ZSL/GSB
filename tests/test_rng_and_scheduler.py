from __future__ import annotations

from card_replay.compare import compare_reports
from card_replay.rng import derive_rng_stream, rng_stream_hash
from card_replay.scheduler import SchedulerBudget, run_scheduled


def test_rng_depends_only_on_seed_seq_delta() -> None:
    first = derive_rng_stream(1234, 7, -3, 8)
    second = derive_rng_stream(1234, 7, -3, 8)
    different_seq = derive_rng_stream(1234, 8, -3, 8)
    different_delta = derive_rng_stream(1234, 7, -2, 8)
    different_seed = derive_rng_stream(1235, 7, -3, 8)

    assert first == second
    assert rng_stream_hash(first) == rng_stream_hash(second)
    assert first != different_seq
    assert first != different_delta
    assert first != different_seed
    assert len(set(first)) > 1


def test_thread_budget_does_not_change_replay_results(generated_bundle) -> None:
    from card_replay.config import load_config
    from card_replay.parser import load_event_stream
    from card_replay.report import build_report

    config_path, events_path = generated_bundle
    config = load_config(config_path)
    parsed = load_event_stream(events_path)

    serial = run_scheduled(
        parsed.ordered_matches(),
        config,
        SchedulerBudget.from_mib(
            threads=1, memory_budget_mib=64, per_match_memory_mib=8
        ),
    )
    parallel = run_scheduled(
        parsed.ordered_matches(),
        config,
        SchedulerBudget.from_mib(
            threads=4, memory_budget_mib=64, per_match_memory_mib=8
        ),
    )
    serial_report = build_report(config=config, parsed=parsed, scheduling=serial)
    parallel_report = build_report(config=config, parsed=parsed, scheduling=parallel)

    assert serial.scheduler["effective_workers"] == 1
    assert parallel.scheduler["effective_workers"] == 4
    compared = compare_reports(serial_report, parallel_report)
    assert compared["consistent"] is True
    assert compared["checks"]["all_event_state_hashes_equal"] is True
    assert compared["checks"]["all_event_traces_equal"] is True


def test_memory_budget_degrades_to_serial_without_missing_results(
    generated_bundle,
) -> None:
    from card_replay.config import load_config
    from card_replay.parser import load_event_stream

    config_path, events_path = generated_bundle
    config = load_config(config_path)
    parsed = load_event_stream(events_path)
    result = run_scheduled(
        parsed.ordered_matches(),
        config,
        SchedulerBudget(
            thread_budget=8,
            memory_budget_bytes=512 * 1024,
            per_match_memory_bytes=8 * 1024 * 1024,
        ),
    )

    assert result.scheduler["effective_workers"] == 1
    assert result.scheduler["degraded"] is True
    assert any(
        item["reason"] == "memory_budget_below_single_match_reservation"
        for item in result.scheduler["degradations"]
    )
    assert result.scheduler["completion"]["completed"] == 6
    assert result.scheduler["completion"]["missing"] == 0
    assert [item.match_id for item in result.matches] == sorted(
        item.match_id for item in result.matches
    )


def test_per_match_memory_denial_is_structured_and_does_not_drop_matches(
    generated_bundle,
) -> None:
    from card_replay.config import load_config
    from card_replay.parser import load_event_stream

    config_path, events_path = generated_bundle
    config = load_config(config_path)
    parsed = load_event_stream(events_path)
    result = run_scheduled(
        parsed.ordered_matches(),
        config,
        SchedulerBudget(
            thread_budget=4,
            memory_budget_bytes=64 * 1024 * 1024,
            per_match_memory_bytes=1,
        ),
    )

    assert result.scheduler["completion"] == {
        "scheduled": 6,
        "completed": 6,
        "denied": 6,
        "missing": 0,
    }
    assert all(
        match.errors[0].code.value == "E_BUDGET_DENIED" for match in result.matches
    )


def test_non_positive_thread_budget_returns_e_budget_denied(generated_bundle) -> None:
    from card_replay.config import load_config
    from card_replay.parser import load_event_stream

    config_path, events_path = generated_bundle
    config = load_config(config_path)
    parsed = load_event_stream(events_path)
    result = run_scheduled(
        parsed.ordered_matches(),
        config,
        SchedulerBudget(
            thread_budget=0,
            memory_budget_bytes=64 * 1024 * 1024,
            per_match_memory_bytes=8 * 1024 * 1024,
        ),
    )

    assert result.scheduler["completion"]["missing"] == 0
    assert all(
        any(error.code.value == "E_BUDGET_DENIED" for error in match.errors)
        for match in result.matches
    )
