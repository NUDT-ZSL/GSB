from __future__ import annotations

from card_replay.config import parse_config
from card_replay.engine import replay_match
from card_replay.errors import ErrorCode
from card_replay.samples import default_rules_config, synthetic_events


def _match(report: dict, match_id: str) -> dict:
    return next(match for match in report["matches"] if match["match_id"] == match_id)


def test_synthetic_bundle_covers_required_business_codes(
    generated_bundle, run_files
) -> None:
    config_path, events_path = generated_bundle
    report = run_files(events_path=events_path, config_path=config_path)

    assert report["summary"]["matches_total"] == 6
    assert report["summary"]["matches_passed"] == 1
    assert report["summary"]["all_matches_have_results"] is True
    assert report["scheduler"]["completion"]["missing"] == 0
    assert report["summary"]["violations_by_code"] == {
        "E_MALFORMED_EVENT": 1,
        "E_RESOURCE_OVERFLOW": 2,
        "E_RNG_DRIFT": 1,
        "E_SEQ_GAP": 1,
        "E_TRIGGER_DEPTH": 1,
    }

    normal = _match(report, "00-normal")
    assert normal["status"] == "ok"
    assert normal["budget"]["overflow_count"] == 0
    assert normal["budget"]["total_cost"] == 5

    gap = _match(report, "01-seq-gap")
    assert gap["events_processed"] == 2
    assert gap["errors"][0]["code"] == "E_SEQ_GAP"
    assert gap["errors"][0]["seq"] == 4

    overflow = _match(report, "02-resource-overflow")
    assert overflow["budget"] == {
        "resource_cap": 10,
        "peak_usage": 13,
        "peak_at_seq": 2,
        "per_player_peak": {"player_a": 13, "player_b": 10},
        "overflow_count": 2,
        "negative_count": 1,
        "above_max_count": 1,
        "first_overflow_seq": 2,
        "total_cost": 22,
    }
    assert [error["seq"] for error in overflow["errors"]] == [2, 3]

    deep = _match(report, "03-deep-trigger")
    assert deep["errors"][0]["code"] == "E_TRIGGER_DEPTH"
    assert deep["traces"][0]["trigger_rolled_back"] is True
    assert len(deep["traces"][0]["trigger_trace"]) == 3
    assert deep["trigger_rollbacks"][0]["discarded_trigger_steps"] == 3
    assert deep["trigger_rollbacks"][0]["rollback_state_hash"] == deep["final_state_hash"]
    assert deep["final_state"]["hp"]["player_b"] == 27
    assert deep["final_state"]["trigger_count"] == 0

    malformed = _match(report, "04-malformed")
    assert malformed["events_processed"] == 1
    assert malformed["errors"][0]["code"] == "E_MALFORMED_EVENT"

    drift = _match(report, "05-rng-drift")
    assert drift["errors"][0]["code"] == "E_RNG_DRIFT"
    assert drift["events_processed"] == 2


def test_duplicate_seq_is_e_seq_gap_and_terminates_match(config) -> None:
    events = [
        {
            "match_id": "dup",
            "seed": 7,
            "seq": 1,
            "actor": "player_a",
            "action": "end_turn",
            "target": "self",
            "cost": 0,
            "rng_seed_delta": 1,
        },
        {
            "match_id": "dup",
            "seed": 7,
            "seq": 2,
            "actor": "player_a",
            "action": "end_turn",
            "target": "self",
            "cost": 0,
            "rng_seed_delta": 2,
        },
        {
            "match_id": "dup",
            "seed": 7,
            "seq": 2,
            "actor": "player_a",
            "action": "end_turn",
            "target": "self",
            "cost": 0,
            "rng_seed_delta": 3,
        },
    ]
    result = replay_match("dup", events, config)
    assert result.status == "violation"
    assert result.events_processed == 2
    assert result.errors[0].code is ErrorCode.SEQ_GAP
    assert result.errors[0].context == {"expected_seq": 3, "actual_seq": 2}
    assert result.traces[-1]["status"] == "rejected"
    assert result.final_state["turn"] == 3


def test_cost_mismatch_is_business_error(config) -> None:
    event = {
        "match_id": "cost",
        "seed": 9,
        "seq": 1,
        "actor": "player_a",
        "action": "play_card",
        "target": "player_b",
        "card_id": "strike",
        "cost": 99,
        "rng_seed_delta": 1,
    }
    result = replay_match("cost", [event], config)
    assert result.events_processed == 0
    assert result.errors[0].code is ErrorCode.COST_MISMATCH
    assert result.errors[0].context["declared_cost"] == 99
    assert result.errors[0].context["configured_cost"] == 2


def test_invalid_field_types_and_unknown_actions_do_not_raise(config) -> None:
    bad_type = {
        "match_id": "bad",
        "seed": 1,
        "seq": 1,
        "actor": "player_a",
        "action": "gain_resource",
        "target": "self",
        "cost": "zero",
        "amount": 1,
        "rng_seed_delta": 1,
    }
    unknown = {
        "match_id": "unknown",
        "seed": 1,
        "seq": 1,
        "actor": "player_a",
        "action": "summon",
        "target": "player_b",
        "cost": 0,
        "rng_seed_delta": 1,
    }
    missing_target = {
        "match_id": "missing",
        "seed": 1,
        "seq": 1,
        "actor": "player_a",
        "action": "end_turn",
        "cost": 0,
        "rng_seed_delta": 1,
    }
    for match_id, event in (
        ("bad", bad_type),
        ("unknown", unknown),
        ("missing", missing_target),
    ):
        result = replay_match(match_id, [event], config)
        assert result.status == "violation"
        assert result.errors[0].code is ErrorCode.MALFORMED_EVENT


def test_trigger_depth_rolls_back_to_chain_start(raw_config) -> None:
    raw_config["trigger_chain_depth"] = 2
    config = parse_config(raw_config)
    deep_event = next(
        candidate
        for candidate in synthetic_events()
        if candidate["match_id"] == "03-deep-trigger"
    )
    result = replay_match("deep", [deep_event], config)
    trace = result.traces[0]
    assert result.errors[0].code is ErrorCode.TRIGGER_DEPTH
    assert trace["trigger_rolled_back"] is True
    assert trace["state"]["hp"]["player_b"] == 27
    assert trace["state"]["resources"]["player_a"] == 8
    assert trace["state"]["trigger_count"] == 0
    assert result.trigger_rollbacks[0]["rollback_state_hash"] == trace["state_hash"]
    assert result.trigger_rollbacks[0]["discarded_state_hash"] != trace["state_hash"]
