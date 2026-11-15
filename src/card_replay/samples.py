from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .canonical import canonical_json
from .rng import derive_rng_stream


def default_rules_config() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "players": {
            "player_a": {"initial_hp": 30, "initial_resource": 10},
            "player_b": {"initial_hp": 30, "initial_resource": 10},
        },
        "cards": {
            "strike": {"cost": 2, "damage": 3},
            "guard": {"cost": 1, "damage": 0},
            "focus": {"cost": 0, "damage": 0, "resource_gain": 2},
            "nova": {"cost": 6, "damage": 8},
            "overload": {"cost": 20, "damage": 1},
        },
        "resource_cap": 10,
        "trigger_chain_depth": 3,
        "rng_stream_size": 4,
        "turn_regeneration": 0,
    }


def _event(
    match_id: str,
    seed: int,
    seq: int,
    actor: str,
    action: str,
    target: str,
    cost: int,
    rng_seed_delta: int,
    **extra: Any,
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "match_id": match_id,
        "seed": seed,
        "seq": seq,
        "actor": actor,
        "action": action,
        "target": target,
        "cost": cost,
        "rng_seed_delta": rng_seed_delta,
    }
    event.update(extra)
    return event


def synthetic_events() -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []

    normal_id = "00-normal"
    normal_seed = 41001
    events.extend(
        [
            _event(
                normal_id,
                normal_seed,
                1,
                "player_a",
                "play_card",
                "player_b",
                2,
                1,
                card_id="strike",
            ),
            _event(
                normal_id,
                normal_seed,
                2,
                "player_a",
                "play_card",
                "player_a",
                1,
                2,
                card_id="guard",
            ),
            _event(
                normal_id,
                normal_seed,
                3,
                "player_a",
                "gain_resource",
                "self",
                0,
                3,
                amount=1,
            ),
            _event(
                normal_id,
                normal_seed,
                4,
                "player_a",
                "end_turn",
                "self",
                0,
                4,
            ),
            _event(
                normal_id,
                normal_seed,
                5,
                "player_a",
                "play_card",
                "player_b",
                2,
                5,
                card_id="strike",
            ),
        ]
    )

    gap_id = "01-seq-gap"
    gap_seed = 42002
    events.extend(
        [
            _event(
                gap_id,
                gap_seed,
                1,
                "player_a",
                "play_card",
                "player_b",
                2,
                11,
                card_id="strike",
            ),
            _event(
                gap_id,
                gap_seed,
                2,
                "player_b",
                "play_card",
                "player_a",
                1,
                12,
                card_id="guard",
            ),
            _event(
                gap_id,
                gap_seed,
                4,
                "player_a",
                "play_card",
                "player_b",
                2,
                14,
                card_id="strike",
            ),
        ]
    )

    overflow_id = "02-resource-overflow"
    overflow_seed = 43003
    events.extend(
        [
            _event(
                overflow_id,
                overflow_seed,
                1,
                "player_a",
                "play_card",
                "player_b",
                2,
                21,
                card_id="strike",
            ),
            _event(
                overflow_id,
                overflow_seed,
                2,
                "player_a",
                "gain_resource",
                "self",
                0,
                22,
                amount=5,
            ),
            _event(
                overflow_id,
                overflow_seed,
                3,
                "player_a",
                "play_card",
                "player_b",
                20,
                24,
                card_id="overload",
            ),
        ]
    )

    deep_id = "03-deep-trigger"
    deep_seed = 44004
    deep_triggers = [
        {
            "action": "damage",
            "target": "player_b",
            "amount": 1,
            "children": [
                {
                    "action": "gain_resource",
                    "target": "self",
                    "amount": 1,
                    "children": [
                        {
                            "action": "damage",
                            "target": "player_b",
                            "amount": 1,
                            "children": [
                                {
                                    "action": "noop",
                                    "target": "self",
                                    "amount": 0,
                                }
                            ],
                        }
                    ],
                }
            ],
        }
    ]
    events.append(
        _event(
            deep_id,
            deep_seed,
            1,
            "player_a",
            "play_card",
            "player_b",
            2,
            31,
            card_id="strike",
            triggers=deep_triggers,
        )
    )

    malformed_id = "04-malformed"
    malformed_seed = 45005
    events.extend(
        [
            _event(
                malformed_id,
                malformed_seed,
                1,
                "player_a",
                "end_turn",
                "self",
                0,
                41,
            ),
            _event(
                malformed_id,
                malformed_seed,
                2,
                "player_a",
                "summon",
                "player_b",
                0,
                42,
            ),
        ]
    )

    drift_id = "05-rng-drift"
    drift_seed = 46006
    correct_stream = derive_rng_stream(drift_seed, 1, 51, 4)
    wrong_stream = list(correct_stream)
    wrong_stream[0] ^= 1
    events.extend(
        [
            _event(
                drift_id,
                drift_seed,
                1,
                "player_a",
                "play_card",
                "player_b",
                2,
                51,
                card_id="strike",
                rng_expected=wrong_stream,
            ),
            _event(
                drift_id,
                drift_seed,
                2,
                "player_a",
                "play_card",
                "player_a",
                1,
                52,
                card_id="guard",
            ),
        ]
    )

    return events


def write_sample_bundle(out_dir: str | Path) -> tuple[Path, Path]:
    destination = Path(out_dir)
    destination.mkdir(parents=True, exist_ok=True)
    config_path = destination / "rules.json"
    events_path = destination / "events.jsonl"

    config_text = json.dumps(
        default_rules_config(),
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    )
    config_path.write_text(config_text + "\n", encoding="utf-8")
    event_text = "".join(canonical_json(event) + "\n" for event in synthetic_events())
    events_path.write_text(event_text, encoding="utf-8")
    return config_path, events_path
