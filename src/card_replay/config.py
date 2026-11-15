from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .errors import ConfigError


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _require_mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{path} must be an object", context={"path": path})
    return value


def _require_string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{path} must be a non-empty string", context={"path": path})
    return value


def _require_int(
    value: Any,
    path: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    if not _is_int(value):
        raise ConfigError(f"{path} must be an integer", context={"path": path})
    if minimum is not None and value < minimum:
        raise ConfigError(
            f"{path} must be >= {minimum}",
            context={"path": path, "actual": value},
        )
    if maximum is not None and value > maximum:
        raise ConfigError(
            f"{path} must be <= {maximum}",
            context={"path": path, "actual": value},
        )
    return value


@dataclass(frozen=True)
class CardRule:
    card_id: str
    cost: int
    damage: int
    resource_gain: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "cost": self.cost,
            "damage": self.damage,
            "resource_gain": self.resource_gain,
        }


@dataclass(frozen=True)
class PlayerRule:
    player_id: str
    initial_hp: int
    initial_resource: int

    def to_dict(self) -> dict[str, int]:
        return {
            "initial_hp": self.initial_hp,
            "initial_resource": self.initial_resource,
        }


@dataclass(frozen=True)
class RulesConfig:
    schema_version: int
    players: dict[str, PlayerRule]
    cards: dict[str, CardRule]
    resource_cap: int
    trigger_chain_depth: int
    rng_stream_size: int
    turn_regeneration: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "players": {
                player_id: rule.to_dict()
                for player_id, rule in sorted(self.players.items())
            },
            "cards": {
                card_id: rule.to_dict()
                for card_id, rule in sorted(self.cards.items())
            },
            "resource_cap": self.resource_cap,
            "trigger_chain_depth": self.trigger_chain_depth,
            "rng_stream_size": self.rng_stream_size,
            "turn_regeneration": self.turn_regeneration,
        }


def parse_config(raw: Mapping[str, Any]) -> RulesConfig:
    root = _require_mapping(raw, "$")
    schema_version = _require_int(
        root.get("schema_version", 1), "$.schema_version", minimum=1
    )

    resource_cap = _require_int(
        root.get("resource_cap"), "$.resource_cap", minimum=1
    )
    trigger_chain_depth = _require_int(
        root.get("trigger_chain_depth"),
        "$.trigger_chain_depth",
        minimum=1,
    )
    rng_stream_size = _require_int(
        root.get("rng_stream_size", 4),
        "$.rng_stream_size",
        minimum=1,
        maximum=256,
    )
    turn_regeneration = _require_int(
        root.get("turn_regeneration", 0),
        "$.turn_regeneration",
        minimum=0,
    )

    raw_players = _require_mapping(root.get("players"), "$.players")
    if not raw_players:
        raise ConfigError("$.players must contain at least one player")
    players: dict[str, PlayerRule] = {}
    for player_id, raw_player in sorted(raw_players.items()):
        player_key = _require_string(player_id, "$.players key")
        player_path = f"$.players.{player_key}"
        player = _require_mapping(raw_player, player_path)
        initial_hp = _require_int(
            player.get("initial_hp"),
            f"{player_path}.initial_hp",
            minimum=1,
        )
        initial_resource = _require_int(
            player.get("initial_resource"),
            f"{player_path}.initial_resource",
            minimum=0,
            maximum=resource_cap,
        )
        players[player_key] = PlayerRule(
            player_id=player_key,
            initial_hp=initial_hp,
            initial_resource=initial_resource,
        )

    raw_cards = _require_mapping(root.get("cards"), "$.cards")
    if not raw_cards:
        raise ConfigError("$.cards must contain at least one card")
    cards: dict[str, CardRule] = {}
    for card_id, raw_card in sorted(raw_cards.items()):
        card_key = _require_string(card_id, "$.cards key")
        card_path = f"$.cards.{card_key}"
        card = _require_mapping(raw_card, card_path)
        cards[card_key] = CardRule(
            card_id=card_key,
            cost=_require_int(card.get("cost"), f"{card_path}.cost", minimum=0),
            damage=_require_int(
                card.get("damage", 0), f"{card_path}.damage", minimum=0
            ),
            resource_gain=_require_int(
                card.get("resource_gain", 0),
                f"{card_path}.resource_gain",
                minimum=0,
            ),
        )

    return RulesConfig(
        schema_version=schema_version,
        players=players,
        cards=cards,
        resource_cap=resource_cap,
        trigger_chain_depth=trigger_chain_depth,
        rng_stream_size=rng_stream_size,
        turn_regeneration=turn_regeneration,
    )


def load_config(path: str | Path) -> RulesConfig:
    with Path(path).open("r", encoding="utf-8") as handle:
        try:
            raw = json.load(handle)
        except json.JSONDecodeError as exc:
            raise ConfigError(
                "rules configuration is not valid JSON",
                context={"line": exc.lineno, "column": exc.colno},
            ) from exc
    return parse_config(raw)
