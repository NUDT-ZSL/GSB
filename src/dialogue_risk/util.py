"""Canonical serialization, hashing and precision helpers."""

from __future__ import annotations

import copy
import hashlib
import json
from decimal import Decimal, ROUND_HALF_UP
from typing import Any


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_bytes(value: Any) -> bytes:
    return canonical_json(value).encode("utf-8")


def digest_value(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(value)).hexdigest()


def quantize(value: float, decimals: int) -> float:
    quantum = Decimal(1).scaleb(-decimals)
    rounded = Decimal(str(float(value))).quantize(quantum, rounding=ROUND_HALF_UP)
    result = float(rounded)
    return 0.0 if result == 0 else result


def quantize_mapping(value: dict[str, Any], decimals: int) -> dict[str, float]:
    return {key: quantize(float(value[key]), decimals) for key in sorted(value)}


def clone(value: Any) -> Any:
    return copy.deepcopy(value)


def message_ids(conversation: dict[str, Any]) -> list[str]:
    return [
        f"{conversation['conv_id']}:{int(message['msg_no'])}"
        for message in sorted(conversation["messages"], key=lambda item: int(item["msg_no"]))
    ]


def flatten_weights(conversations: dict[str, dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    return {
        conv_id: [
            {
                "msg_no": int(message["msg_no"]),
                "weight": float(message.get("weight", 1.0)),
            }
            for message in sorted(
                conversation["messages"], key=lambda item: int(item["msg_no"])
            )
        ]
        for conv_id, conversation in sorted(conversations.items())
    }

