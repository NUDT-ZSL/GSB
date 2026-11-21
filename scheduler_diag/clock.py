from __future__ import annotations

from dataclasses import dataclass


@dataclass
class LogicalClock:
    logical_ms: int
    current_round: int
    round_elapsed_ms: int
    tick_ms: int

    def enter_round(self, round_number: int) -> None:
        if self.current_round != round_number:
            self.current_round = round_number
            self.round_elapsed_ms = 0

    def tick(self, count: int = 1) -> None:
        if count < 0:
            raise ValueError("logical clock tick count must be non-negative")
        amount = count * self.tick_ms
        self.logical_ms += amount
        self.round_elapsed_ms += amount

    def snapshot(self) -> dict[str, int]:
        return {
            "logical_ms": self.logical_ms,
            "current_round": self.current_round,
            "round_elapsed_ms": self.round_elapsed_ms,
        }


def clock_from_state(state: dict[str, int], tick_ms: int) -> LogicalClock:
    return LogicalClock(
        logical_ms=int(state["logical_ms"]),
        current_round=int(state["current_round"]),
        round_elapsed_ms=int(state["round_elapsed_ms"]),
        tick_ms=tick_ms,
    )
