"""Independent fault/interruption injection configuration."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .errors import BusinessError, E_PARAM_OUT_OF_RANGE
from .util import deep_copy_json, load_json


SUPPORTED_INJECTION_TYPES = {
    "nonfinite_input",
    "shape_mismatch",
    "padding_out_of_range",
    "invalid_scale",
    "interrupt",
    "hard_kill",
}


@dataclass(frozen=True)
class InjectionItem:
    id: str
    type: str
    raw: dict[str, Any]

    @property
    def node_id(self) -> str | None:
        value = self.raw.get("node_id", self.raw.get("at_node"))
        return str(value) if value is not None else None


def _validate_item(raw: Any, position: int) -> InjectionItem:
    if not isinstance(raw, dict):
        raise BusinessError(
            E_PARAM_OUT_OF_RANGE,
            f"injections[{position}] must be an object",
            stage="injection_config",
        )
    injection_id = raw.get("id")
    if not isinstance(injection_id, str) or not injection_id:
        raise BusinessError(
            E_PARAM_OUT_OF_RANGE,
            f"injections[{position}].id must be a non-empty string",
            stage="injection_config",
        )
    injection_type = raw.get("type")
    if injection_type not in SUPPORTED_INJECTION_TYPES:
        raise BusinessError(
            E_PARAM_OUT_OF_RANGE,
            f"unsupported injection type {injection_type!r}",
            stage="injection_config",
            details={"injection_id": injection_id},
        )
    if injection_type == "nonfinite_input":
        index = raw.get("index")
        if (
            not isinstance(index, list)
            or len(index) != 4
            or any(isinstance(value, bool) or not isinstance(value, int) for value in index)
        ):
            raise BusinessError(
                E_PARAM_OUT_OF_RANGE,
                "nonfinite_input injection requires a four-integer index",
                stage="injection_config",
                details={"injection_id": injection_id},
            )
    elif injection_type == "shape_mismatch":
        expected = raw.get("expected_shape")
        if (
            not isinstance(expected, list)
            or len(expected) != 4
            or any(isinstance(value, bool) or not isinstance(value, int) or value < 1 for value in expected)
        ):
            raise BusinessError(
                E_PARAM_OUT_OF_RANGE,
                "shape_mismatch injection requires expected_shape with four positive integers",
                stage="injection_config",
                details={"injection_id": injection_id},
            )
        if not isinstance(raw.get("node_id"), str):
            raise BusinessError(
                E_PARAM_OUT_OF_RANGE,
                "shape_mismatch injection requires node_id",
                stage="injection_config",
                details={"injection_id": injection_id},
            )
    elif injection_type in {"padding_out_of_range", "invalid_scale"}:
        field = "padding" if injection_type == "padding_out_of_range" else "scale_factor"
        value = raw.get(field)
        if not isinstance(value, list) or len(value) != 2:
            raise BusinessError(
                E_PARAM_OUT_OF_RANGE,
                f"{injection_type} injection requires {field} with two elements",
                stage="injection_config",
                details={"injection_id": injection_id},
            )
        if not isinstance(raw.get("node_id"), str):
            raise BusinessError(
                E_PARAM_OUT_OF_RANGE,
                f"{injection_type} injection requires node_id",
                stage="injection_config",
                details={"injection_id": injection_id},
            )
    elif injection_type in {"interrupt", "hard_kill"}:
        if not isinstance(raw.get("node_id", raw.get("at_node")), str):
            raise BusinessError(
                E_PARAM_OUT_OF_RANGE,
                f"{injection_type} injection requires node_id or at_node",
                stage="injection_config",
                details={"injection_id": injection_id},
            )
        if injection_type == "interrupt" and raw.get("signal", "SIGTERM") not in {"SIGTERM", "SIGINT"}:
            raise BusinessError(
                E_PARAM_OUT_OF_RANGE,
                "interrupt signal must be SIGTERM or SIGINT",
                stage="injection_config",
                details={"injection_id": injection_id},
            )
        delay = raw.get("delay_seconds", 0.0)
        if isinstance(delay, bool) or not isinstance(delay, (int, float)) or not math.isfinite(float(delay)) or delay < 0:
            raise BusinessError(
                E_PARAM_OUT_OF_RANGE,
                "injection delay_seconds must be non-negative and finite",
                stage="injection_config",
                details={"injection_id": injection_id},
            )
    return InjectionItem(id=injection_id, type=injection_type, raw=deep_copy_json(raw))


def load_injections(
    path: Path | None,
    *,
    enable_ids: Iterable[str] = (),
    disable_ids: Iterable[str] = (),
) -> list[InjectionItem]:
    if path is None:
        return []
    try:
        raw = load_json(path)
    except (OSError, json.JSONDecodeError) as exc:
        raise BusinessError(E_PARAM_OUT_OF_RANGE, f"cannot load injection config: {exc}", stage="injection_config") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("injections", []), list):
        raise BusinessError(E_PARAM_OUT_OF_RANGE, "injection config must contain an injections array", stage="injection_config")
    enable = set(enable_ids)
    disable = set(disable_ids)
    all_items = [_validate_item(item_raw, position) for position, item_raw in enumerate(raw.get("injections", []))]
    all_ids = {item.id for item in all_items}
    unknown = (enable | disable) - all_ids
    if unknown:
        raise BusinessError(
            E_PARAM_OUT_OF_RANGE,
            "unknown injection ID(s) requested",
            stage="injection_config",
            details={"unknown": sorted(unknown)},
        )
    result: list[InjectionItem] = []
    seen: set[str] = set()
    for item in all_items:
        if item.id in seen:
            raise BusinessError(
                E_PARAM_OUT_OF_RANGE,
                f"duplicate injection ID {item.id!r}",
                stage="injection_config",
            )
        seen.add(item.id)
        enabled = bool(item.raw.get("enabled", True))
        if item.id in enable:
            enabled = True
        if item.id in disable:
            enabled = False
        if enabled:
            result.append(item)
    return result


def input_injections(items: list[InjectionItem], tensor_id: str) -> list[InjectionItem]:
    return [
        item
        for item in items
        if item.type == "nonfinite_input" and item.raw.get("tensor_id", tensor_id) == tensor_id
    ]


def node_injections(items: list[InjectionItem], node_id: str) -> dict[str, list[InjectionItem]]:
    grouped: dict[str, list[InjectionItem]] = {}
    for item in items:
        if item.type == "nonfinite_input":
            continue
        if item.node_id == node_id:
            grouped.setdefault(item.type, []).append(item)
    return grouped


def injection_hit(item: InjectionItem, *, stage: str, effect: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": item.id,
        "type": item.type,
        "node_id": item.node_id,
        "stage": stage,
        "effect": effect,
        **extra,
    }
