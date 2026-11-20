"""Stable business codes returned by the component and CLI."""

from __future__ import annotations

from enum import Enum


class BusinessCode(str, Enum):
    OK = "OK"
    E_CONFIG_INVALID = "E_CONFIG_INVALID"
    E_DUP_DIALOG = "E_DUP_DIALOG"
    E_EVENT_TYPE = "E_EVENT_TYPE"
    E_TIMELINE_INVALID = "E_TIMELINE_INVALID"
    E_FRAME_MISMATCH = "E_FRAME_MISMATCH"
    E_EVENT_RANGE = "E_EVENT_RANGE"
    E_OVERLAP = "E_OVERLAP"
    E_OUT_OF_RANGE = "E_OUT_OF_RANGE"
    E_INCONSISTENT = "E_INCONSISTENT"
