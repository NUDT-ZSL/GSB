"""Shared output-block partition and resource-aware block selection."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable

from .errors import DiagnosticError, E_PARAM_INVALID, E_RESOURCE_LIMIT
from .schema import NormalizedJob, STAGE_PARTITION


@dataclass(frozen=True)
class Block:
    batch: int
    h0: int
    h1: int
    w0: int
    w1: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "batch": self.batch,
            "h0": self.h0,
            "h1": self.h1,
            "w0": self.w0,
            "w1": self.w1,
            "elements": (self.h1 - self.h0) * (self.w1 - self.w0),
        }


@dataclass(frozen=True)
class Layout:
    block_size: tuple[int, int]
    blocks: tuple[Block, ...]
    requested_block_size: tuple[int, int]
    dtype: str
    estimate: dict[str, Any]
    selection_basis: str
    resource_events: tuple[dict[str, Any], ...]

    @property
    def block_count(self) -> int:
        return len(self.blocks)

    def as_dict(self) -> dict[str, Any]:
        return {
            "block_size": list(self.block_size),
            "requested_block_size": list(self.requested_block_size),
            "effective_dtype": self.dtype,
            "block_count": self.block_count,
            "blocks": [block.as_dict() for block in self.blocks],
            "estimate": self.estimate,
            "selection_basis": self.selection_basis,
            "resource_events": list(self.resource_events),
        }


def parse_block_size(value: Any, *, job_id: str, field_name: str) -> tuple[int, int]:
    if isinstance(value, int) and not isinstance(value, bool):
        pair = (int(value), int(value))
    elif isinstance(value, (list, tuple)) and len(value) == 2:
        if not all(isinstance(item, int) and not isinstance(item, bool) for item in value):
            raise DiagnosticError(
                E_PARAM_INVALID,
                STAGE_PARTITION,
                job_id,
                field_name,
                "block size entries must be integers",
                {"value": value},
            )
        pair = (int(value[0]), int(value[1]))
    else:
        raise DiagnosticError(
            E_PARAM_INVALID,
            STAGE_PARTITION,
            job_id,
            field_name,
            "block size must be an integer or a pair of integers",
            {"value": value},
        )
    if pair[0] <= 0 or pair[1] <= 0:
        raise DiagnosticError(
            E_PARAM_INVALID,
            STAGE_PARTITION,
            job_id,
            field_name,
            "block size must be strictly positive",
            {"value": list(pair)},
        )
    return pair


def _dedupe_candidates(
    candidates: Iterable[tuple[int, int]]
) -> list[tuple[int, int]]:
    seen: set[tuple[int, int]] = set()
    result: list[tuple[int, int]] = []
    for candidate in candidates:
        if candidate not in seen:
            seen.add(candidate)
            result.append(candidate)
    return result


def candidate_block_sizes(
    job: NormalizedJob,
    *,
    block_policy_override: dict[str, Any] | None = None,
) -> tuple[list[tuple[int, int]], tuple[int, int]]:
    policy = block_policy_override or job.block_policy
    raw_candidates: list[Any] = []
    raw_candidates.append(policy.get("block_size", [32, 32]))
    raw_candidates.extend(policy.get("candidates", []))
    parsed: list[tuple[int, int]] = []
    for index, raw in enumerate(raw_candidates):
        parsed.append(
            parse_block_size(
                raw,
                job_id=job.job_id,
                field_name=(
                    "block_policy.block_size"
                    if index == 0
                    else f"block_policy.candidates[{index - 1}]"
                ),
            )
        )
    requested = parsed[0]

    if job.family == "conv":
        out_h, out_w = job.output_shape[2], job.output_shape[3]
    else:
        out_h, out_w = job.output_shape[1], job.output_shape[2]
    parsed.append((out_h, out_w))
    for block_h, block_w in list(parsed):
        width = block_w
        height = block_h
        while True:
            parsed.append((max(1, height), max(1, width)))
            if height == 1 and width == 1:
                break
            height = max(1, height // 2)
            width = max(1, width // 2)
    parsed.extend([(1, 1)])
    candidates = _dedupe_candidates(parsed)
    selection = str(policy.get("selection", "fixed"))
    if selection == "largest_fitting":
        candidates.sort(key=lambda item: (item[0] * item[1], item[0], item[1]), reverse=True)
    return candidates, requested


def _block_count(
    output_h: int, output_w: int, batch: int, block_h: int, block_w: int
) -> int:
    return batch * math.ceil(output_h / block_h) * math.ceil(output_w / block_w)


def estimate_resources(
    job: NormalizedJob,
    *,
    block_size: tuple[int, int],
    dtype: str,
) -> dict[str, Any]:
    itemsize = 4 if dtype == "float32" else 8
    n, height, width, channels = job.input_shape
    block_h, block_w = block_size
    out_h, out_w = job.output_shape[1], job.output_shape[2]
    out_channels = job.output_shape[1] if job.family == "conv" else channels
    if job.family == "conv":
        out_h, out_w = job.output_shape[2], job.output_shape[3]

    tile_h = min(block_h, out_h)
    tile_w = min(block_w, out_w)
    input_elems = n * height * width * channels
    if job.family == "conv":
        assert job.kernel_shape is not None
        weight_elems = int(
            job.kernel_shape[0]
            * job.kernel_shape[1]
            * job.kernel_shape[2]
            * job.kernel_shape[3]
        )
        receptive_h = (tile_h - 1) * job.stride[0] + job.kernel_shape[2]
        receptive_w = (tile_w - 1) * job.stride[1] + job.kernel_shape[3]
        workspace_input_elems = receptive_h * receptive_w * channels
        output_elems = n * out_channels * out_h * out_w
    else:
        weight_elems = 0
        scale = job.scale or (1.0, 1.0)
        receptive_h = int(math.ceil(tile_h / scale[0])) + 4
        receptive_w = int(math.ceil(tile_w / scale[1])) + 4
        workspace_input_elems = receptive_h * receptive_w * channels
        output_elems = n * out_h * out_w * channels

    tile_output_elems = tile_h * tile_w * out_channels
    live = job.resource_limits.max_live_intermediates
    workspace_bytes = (
        workspace_input_elems + tile_output_elems + weight_elems
    ) * itemsize
    block_count = _block_count(out_h, out_w, n, block_h, block_w)
    reference_bytes = output_elems * 8
    resident_bytes = (
        input_elems * itemsize
        + weight_elems * itemsize
        + output_elems * itemsize
        + reference_bytes
        + workspace_bytes * max(live, 1)
    )
    return {
        "effective_dtype": dtype,
        "itemsize": itemsize,
        "block_size": [block_h, block_w],
        "block_count": block_count,
        "live_intermediates": live,
        "estimated_workspace_bytes": int(workspace_bytes),
        "estimated_resident_bytes": int(resident_bytes),
        "limits": job.resource_limits.as_dict(),
        "reference_output_bytes": int(reference_bytes),
    }


def _fits(estimate: dict[str, Any], job: NormalizedJob) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if estimate["estimated_resident_bytes"] > job.resource_limits.max_resident_bytes:
        reasons.append("max_resident_bytes")
    if estimate["block_count"] > job.resource_limits.max_blocks:
        reasons.append("max_blocks")
    if job.resource_limits.max_live_intermediates < 3:
        reasons.append("max_live_intermediates")
    return not reasons, reasons


def select_layout(
    job: NormalizedJob,
    *,
    block_policy_override: dict[str, Any] | None = None,
) -> Layout:
    candidates, requested = candidate_block_sizes(
        job, block_policy_override=block_policy_override
    )
    limits = job.resource_limits
    events: list[dict[str, Any]] = []
    dtype = job.dtype

    attempts: list[tuple[tuple[int, int], str, dict[str, Any], bool, list[str]]] = []
    for candidate in candidates:
        estimate = estimate_resources(job, block_size=candidate, dtype=dtype)
        fits, reasons = _fits(estimate, job)
        attempts.append((candidate, dtype, estimate, fits, reasons))

    feasible = [attempt for attempt in attempts if attempt[3]]
    dtype_changed = False
    if not feasible and dtype == "float64" and limits.allow_dtype_downgrade:
        dtype = "float32"
        dtype_changed = True
        attempts = []
        for candidate in candidates:
            estimate = estimate_resources(job, block_size=candidate, dtype=dtype)
            fits, reasons = _fits(estimate, job)
            attempts.append((candidate, dtype, estimate, fits, reasons))
        feasible = [attempt for attempt in attempts if attempt[3]]

    if not feasible:
        smallest = min(
            attempts,
            key=lambda item: item[2]["estimated_resident_bytes"],
            default=None,
        )
        raise DiagnosticError(
            E_RESOURCE_LIMIT,
            STAGE_PARTITION,
            job.job_id,
            "resource_limits",
            "no legal block layout satisfies the configured resource limits",
            {
                "limits": limits.as_dict(),
                "best_estimate": smallest[2] if smallest else None,
                "block_candidates": [list(item) for item in candidates],
            },
        )

    requested_estimate = estimate_resources(job, block_size=requested, dtype=job.dtype)
    requested_fits, requested_reasons = _fits(requested_estimate, job)
    requested_feasible = any(
        item[0] == requested and item[1] == dtype and item[3] for item in feasible
    )

    if requested_feasible:
        chosen = next(
            item for item in feasible if item[0] == requested and item[1] == dtype
        )
        basis = "requested block size satisfies all resource limits"
    else:
        requested_area = requested[0] * requested[1]
        chosen = min(
            feasible,
            key=lambda item: (
                abs(item[0][0] * item[0][1] - requested_area),
                -item[0][0] * item[0][1],
            ),
        )
        if dtype_changed:
            basis = "float32 path selected after float64 layout exceeded resource limits"
        elif any(
            job.resource_limits.max_resident_bytes
            < estimate_resources(job, block_size=item[0], dtype=job.dtype)[
                "estimated_resident_bytes"
            ]
            for item in feasible[:1]
        ):
            basis = "block size shrunk to fit max_resident_bytes"
        else:
            basis = "block size adjusted to satisfy block-count and memory limits"

    chosen_block, chosen_dtype, chosen_estimate, _, _ = chosen
    if dtype_changed:
        events.append(
            {
                "code": E_RESOURCE_LIMIT,
                "stage": STAGE_PARTITION,
                "job_id": job.job_id,
                "category": "DTYPE_DOWNGRADE",
                "trigger": "max_resident_bytes",
                "requested_dtype": job.dtype,
                "effective_dtype": chosen_dtype,
                "recovered": True,
            }
        )
    if chosen_block != requested:
        direction = (
            "smaller"
            if chosen_block[0] * chosen_block[1] < requested[0] * requested[1]
            else "larger"
        )
        events.append(
            {
                "code": E_RESOURCE_LIMIT,
                "stage": STAGE_PARTITION,
                "job_id": job.job_id,
                "category": "SMALLER_BLOCK" if direction == "smaller" else "LARGER_BLOCK",
                "trigger": "resource_limits",
                "requested_block_size": list(requested),
                "effective_block_size": list(chosen_block),
                "requested_fit": requested_fits,
                "requested_limit_failures": requested_reasons,
                "recovered": True,
            }
        )

    if job.family == "conv":
        batch_count = job.output_shape[0]
        out_h, out_w = job.output_shape[2], job.output_shape[3]
    else:
        batch_count = job.output_shape[0]
        out_h, out_w = job.output_shape[1], job.output_shape[2]
    blocks = make_blocks(batch_count, out_h, out_w, chosen_block)
    return Layout(
        block_size=chosen_block,
        blocks=tuple(blocks),
        requested_block_size=requested,
        dtype=chosen_dtype,
        estimate=chosen_estimate,
        selection_basis=basis,
        resource_events=tuple(events),
    )


def make_blocks(
    batch_count: int, out_h: int, out_w: int, block_size: tuple[int, int]
) -> list[Block]:
    """Create the shared block grid for all four operators.

    Blocks never extend past the output boundary. Any input halo or padding is
    resolved by the operator's explicitly declared boundary policy inside the
    block computation.
    """

    if out_h <= 0 or out_w <= 0:
        raise ValueError("empty output spatial dimensions")
    block_h, block_w = block_size
    blocks: list[Block] = []
    for batch in range(batch_count):
        for h0 in range(0, out_h, block_h):
            h1 = min(out_h, h0 + block_h)
            for w0 in range(0, out_w, block_w):
                w1 = min(out_w, w0 + block_w)
                blocks.append(Block(batch=batch, h0=h0, h1=h1, w0=w0, w1=w1))
    return blocks
