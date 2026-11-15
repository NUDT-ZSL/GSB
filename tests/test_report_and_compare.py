from __future__ import annotations

import copy

from card_replay.compare import compare_reports
from card_replay.config import parse_config
from card_replay.parser import load_event_stream
from card_replay.report import (
    finalize_report,
    report_content_hash,
    report_to_bytes,
    verify_report_hash,
)
from card_replay.scheduler import SchedulerBudget
from tests.conftest import build_validation


def _budget() -> SchedulerBudget:
    return SchedulerBudget.from_mib(
        threads=4, memory_budget_mib=64, per_match_memory_mib=8
    )


def test_report_is_byte_deterministic_and_hash_verifiable(generated_bundle) -> None:
    config_path, events_path = generated_bundle
    parsed = load_event_stream(events_path)
    from card_replay.config import load_config

    config = load_config(config_path)
    first = build_validation(parsed=parsed, config=config, budget=_budget())
    second = build_validation(parsed=parsed, config=config, budget=_budget())

    first_bytes = report_to_bytes(first)
    second_bytes = report_to_bytes(second)
    assert first_bytes == second_bytes
    assert first["content_hash"] == second["content_hash"]
    assert verify_report_hash(first) is True
    assert report_content_hash(first) == first["content_hash"]

    tampered = copy.deepcopy(first)
    tampered["summary"]["matches_passed"] = 999
    assert verify_report_hash(tampered) is False


def test_same_reports_are_explicitly_consistent(generated_bundle) -> None:
    config_path, events_path = generated_bundle
    parsed = load_event_stream(events_path)
    from card_replay.config import load_config

    config = load_config(config_path)
    left = build_validation(parsed=parsed, config=config, budget=_budget())
    right = build_validation(parsed=parsed, config=config, budget=_budget())
    comparison = compare_reports(left, right)

    assert comparison["verdict"] == "consistent"
    assert comparison["consistent"] is True
    assert comparison["message"] == "ALL_MATCHES_AND_ALL_EVENT_STATE_HASHES_MATCH"
    assert all(comparison["checks"].values())
    assert comparison["first_state_divergence"] is None
    assert comparison["first_trace_divergence"] is None
    assert verify_report_hash(comparison) is True


def test_first_state_divergence_contains_context_and_snapshots(
    generated_bundle, raw_config
) -> None:
    config_path, events_path = generated_bundle
    parsed = load_event_stream(events_path)
    from card_replay.config import load_config

    base_config = load_config(config_path)
    changed_raw = copy.deepcopy(raw_config)
    changed_raw["cards"]["strike"]["damage"] = 4
    changed_config = parse_config(changed_raw)

    left = build_validation(parsed=parsed, config=base_config, budget=_budget())
    right = build_validation(parsed=parsed, config=changed_config, budget=_budget())
    comparison = compare_reports(left, right)

    assert comparison["verdict"] == "divergent"
    assert comparison["message"] == "FIRST_STATE_HASH_DIVERGENCE_FOUND"
    first = comparison["first_state_divergence"]
    assert first["match_id"] == "00-normal"
    assert first["seq"] == 1
    assert first["left_state"]["hp"]["player_b"] == 27
    assert first["right_state"]["hp"]["player_b"] == 26
    assert first["left_state_hash"] != first["right_state_hash"]
    assert first["left_event"]["action"] == "play_card"


def test_equal_trace_counts_are_not_treated_as_consistent(generated_bundle) -> None:
    config_path, events_path = generated_bundle
    parsed = load_event_stream(events_path)
    from card_replay.config import load_config

    config = load_config(config_path)
    left = build_validation(parsed=parsed, config=config, budget=_budget())
    right = copy.deepcopy(left)
    right["matches"][0]["traces"][0]["rng_stream"][0] ^= 1
    right = finalize_report(right)

    assert len(left["matches"][0]["traces"]) == len(right["matches"][0]["traces"])
    comparison = compare_reports(left, right)
    assert comparison["verdict"] == "divergent"
    assert comparison["message"] == "ALL_STATE_HASHES_MATCH_BUT_TRACE_CONTENT_DIFFERS"
    assert comparison["checks"]["event_key_sets_equal"] is True
    assert comparison["checks"]["all_event_state_hashes_equal"] is True
    assert comparison["checks"]["all_event_traces_equal"] is False
    assert comparison["first_state_divergence"] is None
    assert comparison["first_trace_divergence"]["seq"] == 1


def test_missing_event_is_detected_even_with_matching_common_hashes(
    generated_bundle,
) -> None:
    config_path, events_path = generated_bundle
    parsed = load_event_stream(events_path)
    from card_replay.config import load_config

    config = load_config(config_path)
    left = build_validation(parsed=parsed, config=config, budget=_budget())
    right = copy.deepcopy(left)
    right["matches"][0]["traces"].pop()
    right = finalize_report(right)

    comparison = compare_reports(left, right)
    assert comparison["verdict"] == "divergent"
    assert comparison["checks"]["event_key_sets_equal"] is False
    assert comparison["first_trace_divergence"]["kind"] == "missing_event"
