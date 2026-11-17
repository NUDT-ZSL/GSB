"""Immutable records used at the pipeline boundary."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class SlotSpan:
    entity_type: str
    start: int
    end: int
    text: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "type": self.entity_type,
            "start": self.start,
            "end": self.end,
            "text": self.text,
        }


@dataclass(frozen=True)
class Sample:
    sample_id: str
    text: str
    intent: str
    chars: tuple[str, ...]
    spans: tuple[SlotSpan, ...] = ()
    line_number: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "sample_id": self.sample_id,
            "text": self.text,
            "intent": self.intent,
            "chars": list(self.chars),
            "spans": [span.as_dict() for span in self.spans],
            "line_number": self.line_number,
        }

