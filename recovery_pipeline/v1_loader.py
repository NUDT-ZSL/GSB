"""v1 single-file JSON recovery-config loader and validator.

The loader is pure: it never touches persistent state, so a rejected config
leaves nothing behind.
"""

from __future__ import annotations

import json
from pathlib import Path

from .errors import BizError, Err
from .models import V1Action, V1Config, V1Service


def load_v1(path) -> V1Config:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if raw.get("format") != "v1":
        raise BizError(Err.V1_MAPPING_INCONSISTENT, "not a v1 config file",
                       path=str(path), format=raw.get("format"))

    seen: set[str] = set()
    services: list[V1Service] = []
    declared: dict[str, list[str]] = {}
    for entry in raw.get("services", []):
        name = entry.get("name")
        if name in seen:
            raise BizError(Err.V1_DUPLICATE_SERVICE, "duplicate service name",
                           service=name)
        seen.add(name)

        rollback = entry.get("rollback_seconds")
        if not isinstance(rollback, int) or isinstance(rollback, bool) or rollback < 0:
            raise BizError(Err.V1_INVALID_ROLLBACK_SECONDS,
                           "rollback_seconds must be a non-negative integer",
                           service=name, rollback_seconds=rollback)

        actions: list[V1Action] = []
        prev_tick = None
        for step in entry.get("actions", []):
            tick = step.get("at_tick")
            if prev_tick is not None and tick < prev_tick:
                raise BizError(Err.V1_NON_MONOTONIC_STEPS,
                               "action steps must be non-decreasing on the logical clock",
                               service=name, action_id=step.get("id"),
                               at_tick=tick, previous_tick=prev_tick)
            prev_tick = tick
            actions.append(V1Action(action_id=step["id"], at_tick=tick, op=step["op"]))
        declared[name] = [a.action_id for a in actions]
        services.append(V1Service(
            name=name,
            fault_domain=entry.get("fault_domain", ""),
            rollback_seconds=rollback,
            actions=tuple(actions),
        ))

    action_map = raw.get("action_map", {})
    if sorted(action_map) != sorted(declared):
        raise BizError(Err.V1_MAPPING_INCONSISTENT,
                       "flat action_map services do not match declared services",
                       map_only=sorted(set(action_map) - set(declared)),
                       declared_only=sorted(set(declared) - set(action_map)))
    for name, ids in action_map.items():
        if sorted(ids) != sorted(declared[name]):
            raise BizError(Err.V1_MAPPING_INCONSISTENT,
                           "flat action_map entries do not match declared actions",
                           service=name, mapped=sorted(ids),
                           declared=sorted(declared[name]))

    return V1Config(services=tuple(services), action_map=action_map)
