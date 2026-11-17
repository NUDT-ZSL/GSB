"""Scheduling and injection configuration."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from .errors import ConfigInvalidError, InjectionInvalidError
from .util import ROUNDING_MODES, sha256_json


@dataclass(frozen=True)
class ScheduleConfig:
    max_concurrency: int
    global_mac_budget: int
    per_node_mac_quota: int
    cache_residency_bytes: int
    snapshot_commit_interval: int
    first_frame_response_budget_ms: float
    rounding_mode: str = "half_even"
    dequant_decimals: int = 12
    quantization_enabled: bool = True
    debug_node_delay_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def snapshot_config_dict(self) -> dict[str, Any]:
        """Config fields guarded by the snapshot config hash.

        Quantization mode and the debug delay are intentionally excluded: the
        former is an online graph-level edit, while the latter is diagnostic.
        """

        value = asdict(self)
        value.pop("quantization_enabled", None)
        value.pop("debug_node_delay_ms", None)
        return value


def _required(data: dict[str, Any], names: tuple[str, ...], label: str) -> Any:
    for name in names:
        if name in data:
            return data[name]
    raise ConfigInvalidError(f"missing scheduling field: {label}")


def load_schedule_config(path: str | Path) -> ScheduleConfig:
    source = Path(path)
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigInvalidError(f"cannot load schedule config: {exc}") from exc
    return schedule_from_dict(data)


def schedule_from_dict(data: dict[str, Any]) -> ScheduleConfig:
    try:
        max_concurrency = int(
            _required(
                data,
                ("max_concurrency", "max_workers", "thread_pool_max_concurrency"),
                "max_concurrency",
            )
        )
        global_budget = int(
            _required(data, ("global_mac_budget", "global_budget"), "global_mac_budget")
        )
        per_node = int(
            _required(
                data,
                ("per_node_mac_quota", "per_node_budget_quota"),
                "per_node_mac_quota",
            )
        )
        cache_bytes = int(
            _required(
                data,
                ("cache_residency_bytes", "cache_budget_bytes"),
                "cache_residency_bytes",
            )
        )
        snapshot_interval = int(
            _required(data, ("snapshot_commit_interval",), "snapshot_commit_interval")
        )
        first_frame = float(
            _required(
                data,
                ("first_frame_response_budget_ms", "first_frame_budget_ms"),
                "first_frame_response_budget_ms",
            )
        )
        rounding_mode = str(data.get("rounding_mode", "half_even"))
        decimals = int(data.get("dequant_decimals", 12))
        quantization_enabled = bool(data.get("quantization_enabled", True))
        debug_delay = int(data.get("debug_node_delay_ms", 0))
    except ConfigInvalidError:
        raise
    except (TypeError, ValueError) as exc:
        raise ConfigInvalidError(f"invalid scheduling configuration: {exc}") from exc

    if max_concurrency <= 0:
        raise ConfigInvalidError("max_concurrency must be positive")
    if global_budget < 0:
        raise ConfigInvalidError("global_mac_budget must be non-negative")
    if per_node < 0:
        raise ConfigInvalidError("per_node_mac_quota must be non-negative")
    if cache_bytes < 0:
        raise ConfigInvalidError("cache_residency_bytes must be non-negative")
    if snapshot_interval <= 0:
        raise ConfigInvalidError("snapshot_commit_interval must be positive")
    if first_frame < 0:
        raise ConfigInvalidError("first_frame_response_budget_ms must be non-negative")
    if rounding_mode not in ROUNDING_MODES:
        raise ConfigInvalidError(f"unsupported rounding mode: {rounding_mode}")
    if decimals < 0 or decimals > 15:
        raise ConfigInvalidError("dequant_decimals must be between 0 and 15")
    if debug_delay < 0:
        raise ConfigInvalidError("debug_node_delay_ms must be non-negative")
    return ScheduleConfig(
        max_concurrency=max_concurrency,
        global_mac_budget=global_budget,
        per_node_mac_quota=per_node,
        cache_residency_bytes=cache_bytes,
        snapshot_commit_interval=snapshot_interval,
        first_frame_response_budget_ms=first_frame,
        rounding_mode=rounding_mode,
        dequant_decimals=decimals,
        quantization_enabled=quantization_enabled,
        debug_node_delay_ms=debug_delay,
    )


@dataclass(frozen=True)
class InjectionItem:
    id: str
    type: str
    node: str | None = None
    target: str | None = None
    value: Any = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"id": self.id, "type": self.type}
        if self.node is not None:
            result["node"] = self.node
        if self.target is not None:
            result["target"] = self.target
        if self.value is not None:
            result["value"] = self.value
        return result


INJECTION_TYPES = {
    "non_finite_scale",
    "bit_width_out_of_range",
    "accumulator_overflow",
    "cache_budget_zero",
    "node_exception",
}


@dataclass(frozen=True)
class InjectionConfig:
    items: tuple[InjectionItem, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"items": [item.to_dict() for item in self.items]}

    def fingerprint(self) -> str:
        return sha256_json(self.to_dict())

    def items_for_node(self, node_id: str) -> list[InjectionItem]:
        return sorted(
            (item for item in self.items if item.node == node_id), key=lambda item: item.id
        )

    def has_cache_budget_zero(self) -> bool:
        return any(item.type == "cache_budget_zero" for item in self.items)


def load_injection_config(path: str | Path | None) -> InjectionConfig:
    if path is None:
        return InjectionConfig()
    source = Path(path)
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InjectionInvalidError(f"cannot load injection config: {exc}") from exc
    if not isinstance(data, dict):
        raise InjectionInvalidError("injection config root must be an object")
    raw_items = data.get("items", [])
    if not isinstance(raw_items, list):
        raise InjectionInvalidError("injection items must be a list")
    items: list[InjectionItem] = []
    seen: set[str] = set()
    for raw in raw_items:
        if not isinstance(raw, dict):
            raise InjectionInvalidError("injection item must be an object")
        item_id = raw.get("id")
        item_type = raw.get("type")
        if not isinstance(item_id, str) or not item_id:
            raise InjectionInvalidError("injection item id must be a non-empty string")
        if item_id in seen:
            raise InjectionInvalidError(f"duplicate injection id: {item_id}")
        seen.add(item_id)
        if item_type not in INJECTION_TYPES:
            raise InjectionInvalidError(f"unsupported injection type: {item_type}")
        node = raw.get("node")
        target = raw.get("target")
        if node is not None and not isinstance(node, str):
            raise InjectionInvalidError(f"injection {item_id}: node must be a string")
        if target is not None and target not in {"weight", "activation"}:
            raise InjectionInvalidError(
                f"injection {item_id}: target must be weight or activation"
            )
        if item_type != "cache_budget_zero" and not node:
            raise InjectionInvalidError(f"injection {item_id}: node is required")
        items.append(
            InjectionItem(
                id=item_id,
                type=item_type,
                node=node,
                target=target,
                value=raw.get("value"),
            )
        )
    return InjectionConfig(items=tuple(items))


def config_hash(schedule: ScheduleConfig, injection: InjectionConfig) -> str:
    return sha256_json(
        {
            "schedule": schedule.snapshot_config_dict(),
            "injection": injection.to_dict(),
        }
    )


def with_cache_budget(config: ScheduleConfig, bytes_value: int) -> ScheduleConfig:
    return replace(config, cache_residency_bytes=max(0, int(bytes_value)))

