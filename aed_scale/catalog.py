"""Controlled vocabulary used by the AED component."""

from __future__ import annotations

from types import MappingProxyType
from typing import Iterable

EVENT_TYPE_IDS = MappingProxyType(
    {
        "background": 0,
        "cough": 1,
        "keyboard": 2,
        "knock": 3,
        "laughter": 4,
    }
)

ACTIVE_EVENT_TYPES = tuple(
    name for name in sorted(EVENT_TYPE_IDS, key=EVENT_TYPE_IDS.__getitem__) if name != "background"
)

EVENT_TYPE_ORDER = MappingProxyType(
    {name: EVENT_TYPE_IDS[name] for name in EVENT_TYPE_IDS}
)


def event_type_id(event_type: str) -> int:
    """Return the stable numeric id for a controlled event type."""

    return EVENT_TYPE_IDS[event_type]


def controlled_event_types(*, include_background: bool = True) -> Iterable[str]:
    if include_background:
        return tuple(EVENT_TYPE_IDS)
    return ACTIVE_EVENT_TYPES
