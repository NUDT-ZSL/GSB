from __future__ import annotations

from haptic_engine.envelope import (
    build_device_levels,
    event_sample_bounds,
    physical_frame_bounds,
)
from haptic_engine.models import ControlPoint, Device, HapticEvent
from haptic_engine.packing import plan_device, validate_plan

from .conftest import load_bundle


def _device(**updates):
    values = {
        "id": "dev",
        "sample_rate_hz": 1000,
        "frame_duration_ms": 10,
        "strength_min": 0.0,
        "strength_max": 10.0,
        "quantization_levels": 11,
        "max_commands_per_frame": 2,
    }
    values.update(updates)
    return Device(**values)


def _event(**updates):
    values = {
        "id": "event",
        "type": "impact",
        "start_ms": 0,
        "duration_ms": 10,
        "target_device": "dev",
        "envelope": (
            ControlPoint(0.0, 0.0),
            ControlPoint(10.0, 10.0),
        ),
        "anchor": None,
    }
    values.update(updates)
    return HapticEvent(**values)


def test_sample_interval_and_piecewise_linear_quantization():
    device = _device()
    event = _event()
    start, end = event_sample_bounds(device, event)
    assert (start, end) == (0, 10)
    levels = build_device_levels(device, (event,))
    assert levels.tolist() == [0, 1, 2, 3, 4, 5, 6, 7, 8, 9]


def test_event_start_uses_ceil_to_first_sample_at_or_after_start():
    device = _device(sample_rate_hz=800, frame_duration_ms=10)
    event = _event(start_ms=1, duration_ms=1)
    start, end = event_sample_bounds(device, event)
    assert start == 1
    assert end == 2
    assert end - start == 1


def test_frame_bounds_are_monotonic_for_fractional_sample_counts():
    device = _device(sample_rate_hz=333, frame_duration_ms=7)
    bounds = [physical_frame_bounds(device, index) for index in range(20)]
    assert all(left < right for left, right in bounds)
    assert all(bounds[index][1] == bounds[index + 1][0] for index in range(19))


def test_generated_plan_reconstructs_every_sample_and_respects_caps(workspace):
    bundle = load_bundle(workspace)
    for device in bundle.devices:
        plan = plan_device(bundle, device, bundle.events_by_device[device.id])
        validate_plan(plan)
        reconstructed: list[int] = []
        for block in plan.blocks:
            assert block.command_count <= device.max_commands_per_frame
            assert (
                block.payload["completed_sample_start"]
                == block.sample_start
            )
            assert block.payload["completed_sample_end"] == block.sample_end
            for command in block.payload["commands"]:
                assert command["sample_count"] > 0
                reconstructed.extend(
                    [command["level"]] * command["sample_count"]
                )
        assert reconstructed == plan.levels.tolist()
        assert plan.split_count == sum(
            block.payload["split_count"] for block in plan.blocks
        )
        assert plan.split_count > 0


def test_planning_is_repeatable(workspace):
    bundle = load_bundle(workspace)
    for device in bundle.devices:
        first = plan_device(bundle, device, bundle.events_by_device[device.id])
        second = plan_device(bundle, device, bundle.events_by_device[device.id])
        assert first.blocks == second.blocks
        assert first.commands == second.commands


def test_overlapping_events_use_deterministic_max_envelope():
    device = _device(quantization_levels=11)
    low = _event(id="low", envelope=(ControlPoint(0.0, 2.0), ControlPoint(9.0, 2.0)))
    high = _event(id="high", envelope=(ControlPoint(0.0, 8.0), ControlPoint(9.0, 8.0)))
    levels_low_first = build_device_levels(device, (low, high))
    levels_high_first = build_device_levels(device, (high, low))
    assert levels_low_first.tolist() == levels_high_first.tolist()
    assert set(levels_low_first.tolist()) == {8}
