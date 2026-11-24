from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

import numpy as np

from .envelope import (
    build_device_levels,
    physical_frame_bounds,
    quantized_strength,
)
from .errors import canonical_json
from .models import BLOCK_SCHEMA, Bundle, Device, HapticEvent


@dataclass(frozen=True)
class BlockPlan:
    device_id: str
    block_index: int
    command_count: int
    sample_start: int
    sample_end: int
    payload: dict[str, Any]
    digest: str

    @property
    def filename(self) -> str:
        return f"block_{self.block_index:08d}.json"


@dataclass(frozen=True)
class DevicePlan:
    device: Device
    events: tuple[HapticEvent, ...]
    levels: np.ndarray
    blocks: tuple[BlockPlan, ...]
    split_count: int
    commands: tuple[dict[str, Any], ...]


def seal_block(payload: dict[str, Any]) -> tuple[dict[str, Any], str]:
    sealed = dict(payload)
    sealed["digest_algorithm"] = "sha256"
    digest = hashlib.sha256(canonical_json(sealed).encode("utf-8")).hexdigest()
    sealed["digest"] = digest
    return sealed, digest


def block_digest(payload_without_digest: dict[str, Any]) -> str:
    return hashlib.sha256(
        canonical_json(payload_without_digest).encode("utf-8")
    ).hexdigest()


def _runs_for_frame(levels: np.ndarray, frame_start: int) -> list[tuple[int, int, int]]:
    if len(levels) == 0:
        return []
    changes = np.flatnonzero(np.diff(levels) != 0) + 1
    boundaries = np.concatenate(([0], changes, [len(levels)])).astype(np.int64)
    return [
        (
            frame_start + int(boundaries[index]),
            int(boundaries[index + 1] - boundaries[index]),
            int(levels[boundaries[index]]),
        )
        for index in range(len(boundaries) - 1)
    ]


def plan_device(
    bundle: Bundle,
    device: Device,
    events: tuple[HapticEvent, ...],
) -> DevicePlan:
    levels = build_device_levels(device, events)
    blocks: list[BlockPlan] = []
    flat_commands: list[dict[str, Any]] = []
    command_index = 0
    output_frame_index = 0
    source_frame_index = 0
    split_count = 0
    horizon = len(levels)

    while True:
        frame_start, frame_end = physical_frame_bounds(device, source_frame_index)
        if frame_start >= horizon:
            break
        if frame_end <= frame_start:
            source_frame_index += 1
            continue
        frame_end = min(frame_end, horizon)
        frame_levels = levels[frame_start:frame_end]
        runs = _runs_for_frame(frame_levels, frame_start)
        if not runs:
            source_frame_index += 1
            continue
        cap = device.max_commands_per_frame
        chunks = [runs[index : index + cap] for index in range(0, len(runs), cap)]
        segment_count = len(chunks)
        frame_split_count = max(0, segment_count - 1)
        split_count += frame_split_count
        for segment_index, chunk in enumerate(chunks):
            commands: list[dict[str, Any]] = []
            for run_start, run_count, level in chunk:
                commands.append(
                    {
                        "command_index": command_index,
                        "frame_index": output_frame_index,
                        "source_frame_index": source_frame_index,
                        "sample_start": run_start,
                        "sample_end": run_start + run_count,
                        "sample_count": run_count,
                        "level": level,
                        "strength": quantized_strength(device, level),
                    }
                )
                command_index += 1
            command_sample_start = commands[0]["sample_start"]
            command_sample_end = commands[-1]["sample_end"]
            payload = {
                "schema_version": BLOCK_SCHEMA,
                "version_id": bundle.version_id,
                "device_id": device.id,
                "block_index": len(blocks),
                "frame_index": output_frame_index,
                "source_frame_index": source_frame_index,
                "segment_index": segment_index,
                "segment_count": segment_count,
                "continuation": segment_index > 0,
                "sample_start": command_sample_start,
                "sample_end": command_sample_end,
                "sample_count": command_sample_end - command_sample_start,
                "completed_sample_start": command_sample_start,
                "completed_sample_end": command_sample_end,
                "commands": commands,
                "command_count": len(commands),
                "split_count": frame_split_count if segment_index == 0 else 0,
                "split_reason": (
                    "FRAME_COMMAND_LIMIT"
                    if frame_split_count
                    else None
                ),
            }
            sealed, digest = seal_block(payload)
            block = BlockPlan(
                device_id=device.id,
                block_index=len(blocks),
                command_count=len(commands),
                sample_start=command_sample_start,
                sample_end=command_sample_end,
                payload=sealed,
                digest=digest,
            )
            blocks.append(block)
            flat_commands.extend(commands)
            output_frame_index += 1
        source_frame_index += 1
    return DevicePlan(
        device=device,
        events=events,
        levels=levels,
        blocks=tuple(blocks),
        split_count=split_count,
        commands=tuple(flat_commands),
    )


def validate_plan(plan: DevicePlan) -> None:
    expected_sample = 0
    expected_command = 0
    for block in plan.blocks:
        if block.sample_start != expected_sample:
            raise AssertionError("block sample ranges are not contiguous")
        if block.command_count > plan.device.max_commands_per_frame:
            raise AssertionError("block exceeds per-frame command limit")
        if block.sample_end < block.sample_start:
            raise AssertionError("invalid block sample range")
        for command in block.payload["commands"]:
            if command["command_index"] != expected_command:
                raise AssertionError("command indices are not contiguous")
            if command["sample_count"] <= 0:
                raise AssertionError("command has no samples")
            expected_command += 1
        expected_sample = block.sample_end
    if expected_sample != len(plan.levels):
        raise AssertionError("commands do not cover the complete sample timeline")
