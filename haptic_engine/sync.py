from __future__ import annotations

import math
from typing import Any

from .envelope import event_sample_bounds
from .errors import HapticError
from .models import AnchorDecl, Bundle, HapticEvent


def _earliest_marker_event(
    events: tuple[HapticEvent, ...], marker: str
) -> HapticEvent | None:
    candidates = [event for event in events if event.anchor == marker]
    if not candidates:
        return None
    candidates.sort(key=lambda event: (event.start_ms, event.id))
    return candidates[0]


def _command_for_sample(
    commands: tuple[dict[str, Any], ...], sample: int
) -> dict[str, Any] | None:
    for command in commands:
        if command["sample_start"] <= sample < command["sample_end"]:
            return command
    return None


def _round_half_up(value: float) -> int:
    return math.floor(value + 0.5)


def _diagnose_anchor(
    bundle: Bundle,
    anchor: AnchorDecl,
    commands_by_device: dict[str, tuple[dict[str, Any], ...]],
) -> list[dict[str, Any]]:
    events_by_device = bundle.events_by_device
    device_by_id = bundle.device_by_id
    reference_event = _earliest_marker_event(
        events_by_device.get(anchor.reference_device, ()), anchor.marker
    )
    if reference_event is None:
        raise HapticError(
            "E_SYNC",
            "reference device has no event for the anchor marker",
            {
                "anchor_id": anchor.id,
                "marker": anchor.marker,
                "device_id": anchor.reference_device,
                "reason": "missing_reference_event",
            },
        )
    reference_device = device_by_id[anchor.reference_device]
    reference_sample, _ = event_sample_bounds(reference_device, reference_event)
    reference_time_ms = reference_sample * 1000.0 / reference_device.sample_rate_hz
    diagnostics: list[dict[str, Any]] = []
    for device_id in anchor.devices:
        device = device_by_id[device_id]
        event = _earliest_marker_event(events_by_device.get(device_id, ()), anchor.marker)
        if event is None:
            raise HapticError(
                "E_SYNC",
                "device has no event for the anchor marker",
                {
                    "anchor_id": anchor.id,
                    "marker": anchor.marker,
                    "device_id": device_id,
                    "reason": "missing_device_event",
                },
            )
        actual_sample, _ = event_sample_bounds(device, event)
        command = _command_for_sample(commands_by_device.get(device_id, ()), actual_sample)
        if command is None:
            raise HapticError(
                "E_SYNC",
                "anchor sample is not covered by the synthesized command stream",
                {
                    "anchor_id": anchor.id,
                    "marker": anchor.marker,
                    "device_id": device_id,
                    "actual_sample": actual_sample,
                    "reason": "anchor_not_in_stream",
                },
            )
        expected_sample = _round_half_up(
            reference_time_ms * device.sample_rate_hz / 1000.0
        )
        actual_time_ms = actual_sample * 1000.0 / device.sample_rate_hz
        sample_delta = actual_sample - expected_sample
        delta_ms = actual_time_ms - reference_time_ms
        tolerance_ms = (
            anchor.tolerance_ms
            if anchor.tolerance_ms is not None
            else bundle.config.alignment_tolerance_ms
        )
        diagnostic = {
            "anchor_id": anchor.id,
            "marker": anchor.marker,
            "reference_device": anchor.reference_device,
            "device_id": device_id,
            "is_reference": device_id == anchor.reference_device,
            "reference_sample": reference_sample,
            "reference_time_ms": round(reference_time_ms, 12),
            "actual_sample": actual_sample,
            "actual_time_ms": round(actual_time_ms, 12),
            "expected_sample": expected_sample,
            "sample_delta": sample_delta,
            "delta_ms": round(delta_ms, 12),
            "tolerance_ms": tolerance_ms,
            "within_tolerance": abs(delta_ms) <= tolerance_ms,
            "command_index": command["command_index"],
            "command_sample_offset": actual_sample - command["sample_start"],
        }
        diagnostics.append(diagnostic)
        if abs(delta_ms) > tolerance_ms:
            raise HapticError(
                "E_SYNC",
                "anchor alignment exceeds tolerance",
                {
                    "anchor_id": anchor.id,
                    "marker": anchor.marker,
                    "device_id": device_id,
                    "reference_device": anchor.reference_device,
                    "sample_delta": sample_delta,
                    "delta_ms": delta_ms,
                    "tolerance_ms": tolerance_ms,
                    "diagnostic": diagnostic,
                    "anchor_diagnostics": diagnostics,
                },
            )
    return diagnostics


def diagnose_anchors(
    bundle: Bundle,
    commands_by_device: dict[str, tuple[dict[str, Any], ...]],
) -> list[dict[str, Any]]:
    diagnostics: list[dict[str, Any]] = []
    for anchor in bundle.anchors:
        diagnostics.extend(_diagnose_anchor(bundle, anchor, commands_by_device))
    return diagnostics
