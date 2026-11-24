from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import HapticError, canonical_json

ENGINE_SCHEMA = "haptic-stream-engine-v1"
BLOCK_SCHEMA = "haptic-frame-block-v1"
COMMANDS_SCHEMA = "haptic-command-stream-v1"


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _require_dict(value: Any, path: str, code: str = "E_SCHEMA") -> dict[str, Any]:
    if not isinstance(value, dict):
        raise HapticError(code, f"{path} must be an object", {"path": path})
    return value


def _require_list(value: Any, path: str, code: str = "E_SCHEMA") -> list[Any]:
    if not isinstance(value, list):
        raise HapticError(code, f"{path} must be an array", {"path": path})
    return value


def _require_nonempty_str(value: Any, path: str, code: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HapticError(code, f"{path} must be a non-empty string", {"path": path})
    return value


def _require_positive_int(value: Any, path: str, code: str) -> int:
    if not _is_int(value) or value <= 0:
        raise HapticError(code, f"{path} must be a positive integer", {"path": path})
    return value


def _require_nonnegative_int(value: Any, path: str, code: str) -> int:
    if not _is_int(value) or value < 0:
        raise HapticError(code, f"{path} must be a non-negative integer", {"path": path})
    return value


@dataclass(frozen=True)
class Device:
    id: str
    sample_rate_hz: int
    frame_duration_ms: int
    strength_min: float
    strength_max: float
    quantization_levels: int
    max_commands_per_frame: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "sample_rate_hz": self.sample_rate_hz,
            "frame_duration_ms": self.frame_duration_ms,
            "strength_min": self.strength_min,
            "strength_max": self.strength_max,
            "quantization_levels": self.quantization_levels,
            "max_commands_per_frame": self.max_commands_per_frame,
        }


@dataclass(frozen=True)
class ControlPoint:
    offset_ms: float
    strength: float

    def to_dict(self) -> dict[str, Any]:
        return {"offset_ms": self.offset_ms, "strength": self.strength}


@dataclass(frozen=True)
class HapticEvent:
    id: str
    type: str
    start_ms: int
    duration_ms: int
    target_device: str
    envelope: tuple[ControlPoint, ...]
    anchor: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "start_ms": self.start_ms,
            "duration_ms": self.duration_ms,
            "target_device": self.target_device,
            "envelope": [point.to_dict() for point in self.envelope],
            "anchor": self.anchor,
        }


@dataclass(frozen=True)
class AnchorDecl:
    id: str
    marker: str
    reference_device: str
    devices: tuple[str, ...]
    tolerance_ms: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "marker": self.marker,
            "reference_device": self.reference_device,
            "devices": list(self.devices),
            "tolerance_ms": self.tolerance_ms,
        }


@dataclass(frozen=True)
class EngineConfig:
    global_concurrency: int
    inflight_command_limit: int
    alignment_tolerance_ms: float
    state_dir: str
    export_dir: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "global_concurrency": self.global_concurrency,
            "inflight_command_limit": self.inflight_command_limit,
            "alignment_tolerance_ms": self.alignment_tolerance_ms,
            "state_dir": self.state_dir,
            "export_dir": self.export_dir,
        }


@dataclass(frozen=True)
class Bundle:
    workspace: Path
    devices: tuple[Device, ...]
    events: tuple[HapticEvent, ...]
    anchors: tuple[AnchorDecl, ...]
    config: EngineConfig
    manifest_digest: str
    events_digest: str
    version_id: str

    @property
    def device_by_id(self) -> dict[str, Device]:
        return {device.id: device for device in self.devices}

    @property
    def events_by_device(self) -> dict[str, tuple[HapticEvent, ...]]:
        grouped: dict[str, list[HapticEvent]] = {device.id: [] for device in self.devices}
        for event in self.events:
            grouped[event.target_device].append(event)
        return {device_id: tuple(items) for device_id, items in grouped.items()}


def parse_device(raw: Any, path: str) -> Device:
    item = _require_dict(raw, path)
    device_id = _require_nonempty_str(item.get("id"), f"{path}.id", "E_DEVICE")
    sample_rate = _require_positive_int(
        item.get("sample_rate_hz"), f"{path}.sample_rate_hz", "E_RATE"
    )
    frame_duration = _require_positive_int(
        item.get("frame_duration_ms"), f"{path}.frame_duration_ms", "E_FRAME"
    )
    max_commands = _require_positive_int(
        item.get("max_commands_per_frame"),
        f"{path}.max_commands_per_frame",
        "E_MAXCMD",
    )
    levels = item.get("quantization_levels")
    if not _is_int(levels) or levels < 2:
        raise HapticError(
            "E_QUANT",
            f"{path}.quantization_levels must be an integer >= 2",
            {"path": f"{path}.quantization_levels"},
        )
    strength_min = item.get("strength_min")
    strength_max = item.get("strength_max")
    if not _is_number(strength_min) or not _is_number(strength_max):
        raise HapticError(
            "E_RANGE",
            f"{path} strength bounds must be finite numbers",
            {"path": path},
        )
    if float(strength_min) >= float(strength_max):
        raise HapticError(
            "E_RANGE",
            f"{path}.strength_min must be less than strength_max",
            {"path": path},
        )
    return Device(
        id=device_id,
        sample_rate_hz=sample_rate,
        frame_duration_ms=frame_duration,
        strength_min=float(strength_min),
        strength_max=float(strength_max),
        quantization_levels=levels,
        max_commands_per_frame=max_commands,
    )


def parse_event(
    raw: Any,
    path: str,
    device_map: dict[str, Device],
) -> HapticEvent:
    item = _require_dict(raw, path)
    event_id = _require_nonempty_str(item.get("id"), f"{path}.id", "E_EVENT")
    event_type = _require_nonempty_str(item.get("type"), f"{path}.type", "E_EVENT")
    start_ms = _require_nonnegative_int(
        item.get("start_ms"), f"{path}.start_ms", "E_EVENT_START"
    )
    duration_ms = _require_positive_int(
        item.get("duration_ms"), f"{path}.duration_ms", "E_EVENT_DURATION"
    )
    target_device = _require_nonempty_str(
        item.get("target_device"), f"{path}.target_device", "E_TARGET"
    )
    if target_device not in device_map:
        raise HapticError(
            "E_TARGET",
            f"{path}.target_device does not exist",
            {"path": f"{path}.target_device", "target_device": target_device},
        )
    device = device_map[target_device]
    envelope_raw = _require_list(item.get("envelope"), f"{path}.envelope", "E_ENVELOPE")
    if not envelope_raw:
        raise HapticError(
            "E_ENVELOPE", f"{path}.envelope must not be empty", {"path": path}
        )
    points: list[ControlPoint] = []
    previous_offset: float | None = None
    for index, raw_point in enumerate(envelope_raw):
        point_path = f"{path}.envelope[{index}]"
        point = _require_dict(raw_point, point_path, "E_ENVELOPE")
        offset = point.get("offset_ms", point.get("time_ms"))
        strength = point.get("strength")
        if not _is_number(offset) or float(offset) < 0:
            raise HapticError(
                "E_ENVELOPE_TIME",
                f"{point_path} time must be a finite non-negative number",
                {"path": point_path},
            )
        if previous_offset is not None and float(offset) <= previous_offset:
            raise HapticError(
                "E_ENVELOPE_TIME",
                f"{point_path} time must be strictly increasing",
                {"path": point_path, "previous": previous_offset, "current": offset},
            )
        if not _is_number(strength):
            raise HapticError(
                "E_ENVELOPE_RANGE",
                f"{point_path}.strength must be finite",
                {"path": point_path},
            )
        if float(strength) < device.strength_min or float(strength) > device.strength_max:
            raise HapticError(
                "E_ENVELOPE_RANGE",
                f"{point_path}.strength is outside target device range",
                {
                    "path": point_path,
                    "strength": strength,
                    "min": device.strength_min,
                    "max": device.strength_max,
                },
            )
        points.append(ControlPoint(float(offset), float(strength)))
        previous_offset = float(offset)
    anchor = item.get("anchor")
    if anchor is not None:
        anchor = _require_nonempty_str(anchor, f"{path}.anchor", "E_ANCHOR")
    return HapticEvent(
        id=event_id,
        type=event_type,
        start_ms=start_ms,
        duration_ms=duration_ms,
        target_device=target_device,
        envelope=tuple(points),
        anchor=anchor,
    )


def parse_anchor(raw: Any, path: str, device_map: dict[str, Device]) -> AnchorDecl:
    item = _require_dict(raw, path)
    anchor_id = _require_nonempty_str(item.get("id"), f"{path}.id", "E_ANCHOR")
    marker = _require_nonempty_str(item.get("marker"), f"{path}.marker", "E_ANCHOR")
    reference_device = _require_nonempty_str(
        item.get("reference_device"), f"{path}.reference_device", "E_ANCHOR"
    )
    if reference_device not in device_map:
        raise HapticError(
            "E_ANCHOR",
            f"{path}.reference_device does not exist",
            {"path": path, "reference_device": reference_device},
        )
    devices_raw = item.get("devices")
    if devices_raw is None:
        devices = tuple(sorted(device_map))
    else:
        devices_list = _require_list(devices_raw, f"{path}.devices", "E_ANCHOR")
        devices = tuple(sorted(
            _require_nonempty_str(value, f"{path}.devices[]", "E_ANCHOR")
            for value in devices_list
        ))
        if len(set(devices)) != len(devices):
            raise HapticError(
                "E_ANCHOR", f"{path}.devices contains duplicates", {"path": path}
            )
        for device_id in devices:
            if device_id not in device_map:
                raise HapticError(
                    "E_ANCHOR",
                    f"{path}.devices references an unknown device",
                    {"path": path, "device_id": device_id},
                )
        if reference_device not in devices:
            raise HapticError(
                "E_ANCHOR",
                f"{path}.reference_device must be included in devices",
                {"path": path},
            )
    tolerance = item.get("tolerance_ms")
    if tolerance is not None:
        if not _is_number(tolerance) or float(tolerance) < 0:
            raise HapticError(
                "E_ANCHOR",
                f"{path}.tolerance_ms must be a finite non-negative number",
                {"path": path},
            )
        tolerance = float(tolerance)
    return AnchorDecl(
        id=anchor_id,
        marker=marker,
        reference_device=reference_device,
        devices=devices,
        tolerance_ms=tolerance,
    )


def parse_config(raw: Any) -> EngineConfig:
    item = _require_dict(raw, "config")
    concurrency = _require_positive_int(
        item.get("global_concurrency"), "config.global_concurrency", "E_CONFIG"
    )
    inflight = _require_positive_int(
        item.get("inflight_command_limit"),
        "config.inflight_command_limit",
        "E_BUDGET",
    )
    tolerance = item.get("alignment_tolerance_ms")
    if not _is_number(tolerance) or float(tolerance) < 0:
        raise HapticError(
            "E_CONFIG",
            "config.alignment_tolerance_ms must be a finite non-negative number",
            {"path": "config.alignment_tolerance_ms"},
        )
    state_dir = _require_nonempty_str(
        item.get("state_dir"), "config.state_dir", "E_CONFIG"
    )
    export_dir = _require_nonempty_str(
        item.get("export_dir"), "config.export_dir", "E_CONFIG"
    )
    return EngineConfig(
        global_concurrency=concurrency,
        inflight_command_limit=inflight,
        alignment_tolerance_ms=float(tolerance),
        state_dir=state_dir,
        export_dir=export_dir,
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def parse_bundle(
    workspace: Path | str,
    devices_raw: Any,
    events_raw: Any,
    config_raw: Any,
) -> Bundle:
    workspace = Path(workspace).resolve(strict=False)
    devices_doc = _require_dict(devices_raw, "devices")
    devices_list = _require_list(devices_doc.get("devices"), "devices.devices", "E_DEVICE")
    devices: list[Device] = []
    seen_ids: set[str] = set()
    for index, raw_device in enumerate(devices_list):
        device = parse_device(raw_device, f"devices.devices[{index}]")
        if device.id in seen_ids:
            raise HapticError(
                "E_DEVICE",
                "device id must be unique and non-empty",
                {"device_id": device.id, "path": f"devices.devices[{index}].id"},
            )
        seen_ids.add(device.id)
        devices.append(device)
    if not devices:
        raise HapticError("E_DEVICE", "at least one device is required", {})
    devices.sort(key=lambda item: item.id)
    device_map = {device.id: device for device in devices}

    events_doc = _require_dict(events_raw, "events")
    events_list = _require_list(events_doc.get("events"), "events.events", "E_EVENT")
    events: list[HapticEvent] = []
    event_ids: set[str] = set()
    for index, raw_event in enumerate(events_list):
        event = parse_event(raw_event, f"events.events[{index}]", device_map)
        if event.id in event_ids:
            raise HapticError(
                "E_EVENT",
                "event id must be unique and non-empty",
                {"event_id": event.id, "path": f"events.events[{index}].id"},
            )
        event_ids.add(event.id)
        events.append(event)
    events.sort(key=lambda item: (item.start_ms, item.target_device, item.id))

    anchors_raw = events_doc.get("anchors", [])
    anchors_list = _require_list(anchors_raw, "events.anchors", "E_ANCHOR")
    anchors: list[AnchorDecl] = []
    anchor_ids: set[str] = set()
    for index, raw_anchor in enumerate(anchors_list):
        anchor = parse_anchor(raw_anchor, f"events.anchors[{index}]", device_map)
        if anchor.id in anchor_ids:
            raise HapticError(
                "E_ANCHOR",
                "anchor id must be unique",
                {"anchor_id": anchor.id, "path": f"events.anchors[{index}].id"},
            )
        anchor_ids.add(anchor.id)
        anchors.append(anchor)
    config = parse_config(config_raw)
    max_frame_commands = max(device.max_commands_per_frame for device in devices)
    if config.inflight_command_limit < max_frame_commands:
        raise HapticError(
            "E_BUDGET",
            "inflight_command_limit must be at least the largest per-frame command cap",
            {
                "inflight_command_limit": config.inflight_command_limit,
                "required": max_frame_commands,
            },
        )

    from .state import validate_workspace_paths

    validate_workspace_paths(workspace, config.state_dir, config.export_dir)

    normalized_devices = [device.to_dict() for device in devices]
    normalized_events = {
        "events": [event.to_dict() for event in events],
        "anchors": [
            anchor.to_dict()
            for anchor in sorted(anchors, key=lambda item: (item.marker, item.id))
        ],
    }
    manifest_digest = _digest(normalized_devices)
    events_digest = _digest(normalized_events)
    version_id = _digest(
        {
            "engine_schema": ENGINE_SCHEMA,
            "manifest_digest": manifest_digest,
            "events_digest": events_digest,
        }
    )
    return Bundle(
        workspace=workspace,
        devices=tuple(devices),
        events=tuple(events),
        anchors=tuple(anchors),
        config=config,
        manifest_digest=manifest_digest,
        events_digest=events_digest,
        version_id=version_id,
    )
