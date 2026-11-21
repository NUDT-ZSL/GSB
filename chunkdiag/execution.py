"""Shared block execution for convolution and interpolation."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from .partition import Block, Layout
from .reference import _align_corners_false, _clamped, _cubic_weight
from .schema import NormalizedJob


@dataclass(frozen=True)
class ExecutionStats:
    output: np.ndarray
    block_records: tuple[dict[str, Any], ...]
    boundary_policy: dict[str, Any]
    dtype: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "dtype": self.dtype,
            "block_count": len(self.block_records),
            "blocks": list(self.block_records),
            "boundary_policy": self.boundary_policy,
        }


def _padded_input(job: NormalizedJob, input_array: np.ndarray, dtype: str) -> np.ndarray:
    np_dtype = np.dtype(dtype)
    arrays = np.asarray(input_array).astype(np_dtype, copy=False)
    n, height, width, channels = arrays.shape
    top, bottom, left, right = job.padding
    padded = np.zeros(
        (n, height + top + bottom, width + left + right, channels),
        dtype=np_dtype,
    )
    if height and width and channels:
        padded[:, top : top + height, left : left + width, :] = arrays
    return padded


def _conv_block(
    job: NormalizedJob,
    padded: np.ndarray,
    weights: np.ndarray,
    block: Block,
    output: np.ndarray,
) -> None:
    assert job.kernel_shape is not None
    out_channels, _, kernel_h, kernel_w = job.kernel_shape
    stride_h, stride_w = job.stride
    channels = job.input_shape[3]
    for out_channel in range(out_channels):
        weight = weights[out_channel].astype(np.float64, copy=False).transpose(1, 2, 0)
        for oh in range(block.h0, block.h1):
            input_y = oh * stride_h
            for ow in range(block.w0, block.w1):
                input_x = ow * stride_w
                tile = padded[
                    block.batch,
                    input_y : input_y + kernel_h,
                    input_x : input_x + kernel_w,
                    :,
                ]
                contribution = tile.astype(np.float64, copy=False) * weight
                output[block.batch, out_channel, oh, ow] = np.sum(
                    contribution, dtype=np.float64
                )


def _interp_block(
    job: NormalizedJob,
    padded: np.ndarray,
    block: Block,
    output: np.ndarray,
) -> None:
    assert job.scale is not None
    scale_h, scale_w = job.scale
    padded_h, padded_w = padded.shape[1], padded.shape[2]
    mode = job.interp_mode
    for oh in range(block.h0, block.h1):
        source_y = _align_corners_false(oh, scale_h)
        if mode == "nearest":
            iy = _clamped(math.floor(source_y + 0.5), 0, padded_h - 1)
        else:
            y0 = math.floor(source_y)
            fy = source_y - y0
            if mode == "bilinear":
                y0c = _clamped(y0, 0, padded_h - 1)
                y1c = _clamped(y0 + 1, 0, padded_h - 1)
            else:
                y_indices = [
                    _clamped(y0 + offset, 0, padded_h - 1)
                    for offset in (-1, 0, 1, 2)
                ]
                wy = [_cubic_weight(fy - offset) for offset in (-1, 0, 1, 2)]
        for ow in range(block.w0, block.w1):
            source_x = _align_corners_false(ow, scale_w)
            if mode == "nearest":
                ix = _clamped(math.floor(source_x + 0.5), 0, padded_w - 1)
                output[block.batch, oh, ow, :] = padded[
                    block.batch, iy, ix, :
                ]
                continue

            x0 = math.floor(source_x)
            fx = source_x - x0
            if mode == "bilinear":
                x0c = _clamped(x0, 0, padded_w - 1)
                x1c = _clamped(x0 + 1, 0, padded_w - 1)
                v_top = padded[block.batch, y0c, x0c, :].astype(
                    np.float64, copy=False
                ) + fx * (
                    padded[block.batch, y0c, x1c, :].astype(
                        np.float64, copy=False
                    )
                    - padded[block.batch, y0c, x0c, :].astype(
                        np.float64, copy=False
                    )
                )
                v_bottom = padded[block.batch, y1c, x0c, :].astype(
                    np.float64, copy=False
                ) + fx * (
                    padded[block.batch, y1c, x1c, :].astype(
                        np.float64, copy=False
                    )
                    - padded[block.batch, y1c, x0c, :].astype(
                        np.float64, copy=False
                    )
                )
                value = v_top + fy * (v_bottom - v_top)
            else:
                x_indices = [
                    _clamped(x0 + offset, 0, padded_w - 1)
                    for offset in (-1, 0, 1, 2)
                ]
                wx = [_cubic_weight(fx - offset) for offset in (-1, 0, 1, 2)]
                rows = []
                for row_index in range(4):
                    row = np.zeros(padded.shape[3], dtype=np.float64)
                    for column in range(4):
                        row = row + wx[column] * padded[
                            block.batch,
                            y_indices[row_index],
                            x_indices[column],
                            :,
                        ].astype(np.float64, copy=False)
                    rows.append(row)
                value = np.zeros(padded.shape[3], dtype=np.float64)
                for row_index in range(4):
                    value = value + wy[row_index] * rows[row_index]
            output[block.batch, oh, ow, :] = value


def execute_chunked(
    job: NormalizedJob,
    input_array: np.ndarray,
    weight_array: np.ndarray | None,
    layout: Layout,
) -> ExecutionStats:
    np_dtype = np.dtype(layout.dtype)
    padded = _padded_input(job, input_array, layout.dtype)
    output = np.zeros(job.output_shape, dtype=np_dtype)
    weights: np.ndarray | None = None
    if job.family == "conv":
        assert weight_array is not None
        weights = np.asarray(weight_array).astype(np_dtype, copy=False)

    if job.family == "conv":
        spatial_h, spatial_w = job.output_shape[2], job.output_shape[3]
    else:
        spatial_h, spatial_w = job.output_shape[1], job.output_shape[2]
    block_records: list[dict[str, Any]] = []
    for index, block in enumerate(layout.blocks):
        if job.family == "conv":
            assert weights is not None
            _conv_block(job, padded, weights, block, output)
        else:
            _interp_block(job, padded, block, output)
        block_records.append(
            {
                "index": index,
                "batch": block.batch,
                "h0": block.h0,
                "h1": block.h1,
                "w0": block.w0,
                "w1": block.w1,
                "edge_block": (
                    block.h0 == 0
                    or block.w0 == 0
                    or block.h1 == spatial_h
                    or block.w1 == spatial_w
                ),
            }
        )

    boundary_policy = {
        "shared_partition": "output_spatial_blocks",
        "conv": "zero_padding",
        "interpolate": "clamp_neighbor_indices",
        "edge_block": "clip output ranges; never compute outside output shape",
        "interp_coordinate_rule": "align_corners_false",
        "bicubic_kernel": "keys_a=-0.75",
    }
    return ExecutionStats(
        output=output,
        block_records=tuple(block_records),
        boundary_policy=boundary_policy,
        dtype=layout.dtype,
    )
