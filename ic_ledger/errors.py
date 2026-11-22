"""Stable business errors."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class BusinessError(Exception):
    code: str
    message: str
    context: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "context": self.context,
            "message": self.message,
        }


class ErrorCode:
    INPUT_NOT_FOUND = "E_INPUT_NOT_FOUND"
    INPUT_JSON = "E_INPUT_JSON"
    SCHEMA = "E_SCHEMA"
    IFACE_NAME = "E_IFACE_NAME"
    IFACE_DUPLICATE = "E_IFACE_DUPLICATE"
    QUEUE_INDEX = "E_QUEUE_INDEX"
    QUEUE_DUPLICATE = "E_QUEUE_DUPLICATE"
    QUEUE_WEIGHT = "E_QUEUE_WEIGHT"
    MAX_IRQ_RATE = "E_MAX_IRQ_RATE"
    RX_USECS = "E_RX_USECS"
    TX_USECS = "E_TX_USECS"
    RX_FRAMES = "E_RX_FRAMES"
    TX_FRAMES = "E_TX_FRAMES"
    PARAM_DUPLICATE_CONFLICT = "E_PARAM_DUPLICATE_CONFLICT"
    RECORD_SEQ = "E_RECORD_SEQ"
    RECORD_QUEUE = "E_RECORD_QUEUE"
    ARRIVAL_FRAMES = "E_ARRIVAL_FRAMES"
    FRAME_BYTES = "E_FRAME_BYTES"
    RECORD_CLOCK = "E_RECORD_CLOCK"
    RECORD_DIRECTION = "E_RECORD_DIRECTION"
    CONFIG_FIXED_SCALE = "E_CONFIG_FIXED_SCALE"
    CONFIG_FRAME_INTERVAL = "E_CONFIG_FRAME_INTERVAL"
    CONFIG_TOLERANCE = "E_CONFIG_TOLERANCE"
    CONFIG_IRQ_BUDGET = "E_CONFIG_IRQ_BUDGET"
    CONFIG_ERROR_BOUND = "E_CONFIG_ERROR_BOUND"
    CONFIG_CLOCK_STEP = "E_CONFIG_CLOCK_STEP"
    PARAM_VERSION_ROLLBACK = "E_PARAM_VERSION_ROLLBACK"
    PARAM_VERSION_CONFLICT = "E_PARAM_VERSION_CONFLICT"
    FLOW_CONFLICT = "E_FLOW_CONFLICT"
    QUEUE_SELECTION = "E_QUEUE_SELECTION"
    QUEUE_NOT_FOUND = "E_QUEUE_NOT_FOUND"
    RECORD_OUT_OF_ORDER = "E_RECORD_OUT_OF_ORDER"
    RECORD_NOT_FOUND = "E_RECORD_NOT_FOUND"
    STORE_SCHEMA = "E_STORE_SCHEMA"
    STORE_LOCK = "E_STORE_LOCK"
    VERIFY_FAILED = "E_VERIFY_FAILED"
    USAGE = "E_USAGE"
    INTERNAL = "E_INTERNAL"


def business_error(code: str, message: str, **context: Any) -> BusinessError:
    return BusinessError(code=code, message=message, context=context)

