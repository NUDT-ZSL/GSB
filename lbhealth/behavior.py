"""Deterministic on-disk behavior plans for the loopback simulator."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

_ACTIONS = {"ok", "fail", "slow", "close"}


def behavior_digest(value: dict[str, Any]) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_behavior(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("behavior must be a JSON object")
    default = value.get("default", "ok")
    if default not in _ACTIONS:
        raise ValueError(f"behavior.default must be one of {sorted(_ACTIONS)}")
    start_index = value.get("start_index", 0)
    if not isinstance(start_index, int) or isinstance(start_index, bool) or start_index < 0:
        raise ValueError("behavior.start_index must be a non-negative integer")
    actions = value.get("actions", [])
    if not isinstance(actions, list):
        raise ValueError("behavior.actions must be an array")
    normalized_actions: list[str] = []
    for index, action in enumerate(actions):
        if action not in _ACTIONS:
            raise ValueError(
                f"behavior.actions[{index}] must be one of {sorted(_ACTIONS)}"
            )
        normalized_actions.append(str(action))
    slow_ms = value.get("slow_ms", 200)
    if not isinstance(slow_ms, int) or isinstance(slow_ms, bool) or slow_ms < 0:
        raise ValueError("behavior.slow_ms must be a non-negative integer")
    return {
        "version": 1,
        "start_index": int(start_index),
        "default": str(default),
        "actions": normalized_actions,
        "slow_ms": int(slow_ms),
    }


def read_behavior(path: str | Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as handle:
        return validate_behavior(json.load(handle))


def write_behavior(path: str | Path, value: Any) -> dict[str, Any]:
    normalized = validate_behavior(value)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(normalized, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, target)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise
    return normalized


class BehaviorSource:
    """Reloads plans atomically on every request and retains the last good plan."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._value = read_behavior(self.path)

    def for_request(self, request_index: int) -> str:
        with self._lock:
            try:
                self._value = read_behavior(self.path)
            except (OSError, ValueError, json.JSONDecodeError):
                pass
            value = self._value
        local_index = request_index - int(value["start_index"])
        actions = value["actions"]
        if 0 <= local_index < len(actions):
            return str(actions[local_index])
        return str(value["default"])

    def slow_ms(self) -> int:
        with self._lock:
            return int(self._value["slow_ms"])
