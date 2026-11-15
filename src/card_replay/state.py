from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .canonical import sha256_value
from .config import RulesConfig


@dataclass
class GameState:
    hp: dict[str, int]
    resources: dict[str, int]
    turn: int = 1
    total_cost: int = 0
    played_cards: dict[str, int] = field(default_factory=dict)
    trigger_count: int = 0
    event_count: int = 0

    @classmethod
    def initial(cls, config: RulesConfig) -> "GameState":
        return cls(
            hp={player_id: rule.initial_hp for player_id, rule in config.players.items()},
            resources={
                player_id: rule.initial_resource
                for player_id, rule in config.players.items()
            },
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "hp": {key: self.hp[key] for key in sorted(self.hp)},
            "resources": {key: self.resources[key] for key in sorted(self.resources)},
            "turn": self.turn,
            "total_cost": self.total_cost,
            "played_cards": {
                key: self.played_cards[key] for key in sorted(self.played_cards)
            },
            "trigger_count": self.trigger_count,
            "event_count": self.event_count,
        }

    def state_hash(self) -> str:
        return sha256_value(self.to_dict())


@dataclass
class ResourceBudgetTracker:
    resource_cap: int
    peak_usage: int
    peak_at_seq: int = 0
    per_player_peak: dict[str, int] = field(default_factory=dict)
    overflow_count: int = 0
    negative_count: int = 0
    above_max_count: int = 0
    first_overflow_seq: int | None = None
    total_cost: int = 0
    _overflowed_seqs: set[int] = field(default_factory=set, repr=False)
    _negative_seqs: set[int] = field(default_factory=set, repr=False)
    _above_max_seqs: set[int] = field(default_factory=set, repr=False)

    @classmethod
    def initial(cls, state: GameState, resource_cap: int) -> "ResourceBudgetTracker":
        peaks = {player_id: value for player_id, value in state.resources.items()}
        return cls(
            resource_cap=resource_cap,
            peak_usage=max(peaks.values()) if peaks else 0,
            per_player_peak=peaks,
        )

    def observe(self, state: GameState, seq: int) -> dict[str, list[str]]:
        violations: dict[str, list[str]] = {}
        for player_id, value in sorted(state.resources.items()):
            if value > self.per_player_peak.get(player_id, value):
                self.per_player_peak[player_id] = value
            if value > self.peak_usage:
                self.peak_usage = value
                self.peak_at_seq = seq
            if value < 0:
                violations.setdefault("negative", []).append(player_id)
            if value > self.resource_cap:
                violations.setdefault("above_max", []).append(player_id)
        if violations:
            if "negative" in violations and seq not in self._negative_seqs:
                self._negative_seqs.add(seq)
                self.negative_count += 1
            if "above_max" in violations and seq not in self._above_max_seqs:
                self._above_max_seqs.add(seq)
                self.above_max_count += 1
            if seq not in self._overflowed_seqs:
                self._overflowed_seqs.add(seq)
                self.overflow_count += 1
                if self.first_overflow_seq is None:
                    self.first_overflow_seq = seq
        return violations

    def to_dict(self) -> dict[str, Any]:
        return {
            "resource_cap": self.resource_cap,
            "peak_usage": self.peak_usage,
            "peak_at_seq": self.peak_at_seq,
            "per_player_peak": {
                key: self.per_player_peak[key] for key in sorted(self.per_player_peak)
            },
            "overflow_count": self.overflow_count,
            "negative_count": self.negative_count,
            "above_max_count": self.above_max_count,
            "first_overflow_seq": self.first_overflow_seq,
            "total_cost": self.total_cost,
        }
