"""Serialization result types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .model import Failure, LossItem


@dataclass
class SerializationResult:
    target_version: str
    text: str
    losses: list[LossItem] = field(default_factory=list)
    failures: list[Failure] = field(default_factory=list)
    sidecar: dict[str, Any] | None = None
