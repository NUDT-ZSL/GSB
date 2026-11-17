"""Stable business codes used by the engine, CLI, UI, and logs."""

from __future__ import annotations

from enum import StrEnum


class BusinessCode(StrEnum):
    OK = "OK"
    SWITCHED = "SWITCHED"
    OK_REPLAY = "OK_REPLAY"
    E_SCENE = "E_SCENE"
    E_PROBE = "E_PROBE"
    E_COOLDOWN = "E_COOLDOWN"
    E_BUSY = "E_BUSY"
    E_ISOLATE = "E_ISOLATE"
    E_SNAPSHOT = "E_SNAPSHOT"
    E_REPLAY = "E_REPLAY"
    E_MANUAL_PENDING = "E_MANUAL_PENDING"
    E_NO_TARGET = "E_NO_TARGET"


ERROR_CODES = {
    BusinessCode.E_SCENE,
    BusinessCode.E_PROBE,
    BusinessCode.E_COOLDOWN,
    BusinessCode.E_BUSY,
    BusinessCode.E_ISOLATE,
    BusinessCode.E_SNAPSHOT,
    BusinessCode.E_REPLAY,
    BusinessCode.E_MANUAL_PENDING,
    BusinessCode.E_NO_TARGET,
}
