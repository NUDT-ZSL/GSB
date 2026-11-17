from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class Event:
    event_id: str
    trace_id: str
    actor: str
    action: str
    object: str
    ts: int
    seq: int
    parent_event_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "trace_id": self.trace_id,
            "actor": self.actor,
            "action": self.action,
            "object": self.object,
            "ts": self.ts,
            "seq": self.seq,
            "parent_event_id": self.parent_event_id,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Event":
        return cls(
            event_id=value["event_id"],
            trace_id=value["trace_id"],
            actor=value["actor"],
            action=value["action"],
            object=value["object"],
            ts=value["ts"],
            seq=value["seq"],
            parent_event_id=value.get("parent_event_id"),
        )

    @property
    def canonical_key(self) -> tuple[int, str, int, str]:
        return (self.ts, self.trace_id, self.seq, self.event_id)


def canonical_event(event: Event) -> dict[str, Any]:
    return event.as_dict()

