"""Independent float64 element-wise reference operators.

These functions deliberately avoid the chunked execution module. They are the
truth source for comparisons and are implemented with deterministic Python
loops over NumPy float64 scalars.
"""

from __future__ import annotations

import math

import numpy as np

from .schema import NormalizedJob


def _zero_pad(array: np.ndarray, padding: tuple[int, int, int, int]) -> np.ndarray:
    top, bottom, left, right = padding
    n, height, width, channels = array.shape
    padded = np.zeros(
        (n, height + top + bottom, width + left + right, channels),
        dtype=np.float64,
    )
    if height and width and channels:
        padded[:, top : top + height, left : left + width, :] = array.astype(
            np.float64, copy=False
        )
    return padded


def reference_conv2d(
    job: NormalizedJob, input_array: np.ndarray, weight_array: np.ndarray
) -> np.ndarray:
    assert job.kernel_shape is not None
    n, height, width, channels = job.input_shape
    out_channels, kernel_channels, kernel_h, kernel_w = job.kernel_shape
    stride_h, stride_w = job.stride
    top, _, left, _ = job.padding
    out_h, out_w = job.output_shape[2], job.output_shape[3]

    source = np.asarray(input_array, dtype=np.float64)
    weights = np.asarray(weight_array, dtype=np.float64)
    output = np.empty((n, out_channels, out_h, out_w), dtype=np.float64)
    for batch in range(n):
        for out_channel in range(out_channels):
            for oh in range(out_h):
                for ow in range(out_w):
                    accumulator = np.float64(0.0)
                    for in_channel in range(kernel_channels):
                        for kh in range(kernel_h):
                            for kw in range(kernel_w):
                                ih = oh * stride_h - top + kh
                                iw = ow * stride_w - left + kw
                                if 0 <= ih < height and 0 <= iw < width:
                                    accumulator = np.float64(
                                        accumulator
                                        + np.float64(
                                            source[batch, ih, iw, in_channel]
                                        )
                                        * np.float64(
                                            weights[out_channel, in_channel, kh, kw]
                                        )
                                    )
                    output[batch, out_channel, oh, ow] = accumulator
    return output


def _align_corners_false(index: int, scale: float) -> float:
    return (index + 0.5) / scale - 0.5


def _clamped(value: int, low: int, high: int) -> int:
    if value < low:
        return low
    if value > high:
        return high
    return value


def _cubic_weight(distance: float) -> float:
    """Keys cubic convolution with a=-0.75."""

    a = -0.75
    x = abs(distance)
    if x <= 1.0:
        return ((a + 2.0) * x - (a + 3.0)) * x * x + 1.0
    if x < 2.0:
        return (((x - 5.0) * x + 8.0) * x - 4.0) * a
    return 0.0


def reference_nearest(job: NormalizedJob, input_array: np.ndarray) -> np.ndarray:
    assert job.scale is not None
    source = _zero_pad(input_array, job.padding)
    n, out_h, out_w, channels = job.output_shape
    padded_h, padded_w = source.shape[1], source.shape[2]
    output = np.empty(job.output_shape, dtype=np.float64)
    scale_h, scale_w = job.scale
    for batch in range(n):
        for oh in range(out_h):
            source_y = _align_corners_false(oh, scale_h)
            iy = _clamped(math.floor(source_y + 0.5), 0, padded_h - 1)
            for ow in range(out_w):
                source_x = _align_corners_false(ow, scale_w)
                ix = _clamped(math.floor(source_x + 0.5), 0, padded_w - 1)
                for channel in range(channels):
                    output[batch, oh, ow, channel] = np.float64(
                        source[batch, iy, ix, channel]
                    )
    return output


def reference_bilinear(
    job: NormalizedJob,
    input_array: np.ndarray,
    *,
    weight_dtype: np.dtype | type = np.float64,
) -> np.ndarray:
    assert job.scale is not None
    source = _zero_pad(input_array, job.padding)
    n, out_h, out_w, channels = job.output_shape
    padded_h, padded_w = source.shape[1], source.shape[2]
    output = np.empty(job.output_shape, dtype=np.float64)
    scale_h, scale_w = job.scale
    for batch in range(n):
        for oh in range(out_h):
            source_y = _align_corners_false(oh, scale_h)
            y0 = math.floor(source_y)
            wy = source_y - y0
            y0c = _clamped(y0, 0, padded_h - 1)
            y1c = _clamped(y0 + 1, 0, padded_h - 1)
            for ow in range(out_w):
                source_x = _align_corners_false(ow, scale_w)
                x0 = math.floor(source_x)
                wx = source_x - x0
                x0c = _clamped(x0, 0, padded_w - 1)
                x1c = _clamped(x0 + 1, 0, padded_w - 1)
                wx_w = weight_dtype(wx)
                wy_w = weight_dtype(wy)
                one_minus_x = weight_dtype(1.0) - wx_w
                one_minus_y = weight_dtype(1.0) - wy_w
                for channel in range(channels):
                    v00 = np.float64(source[batch, y0c, x0c, channel])
                    v01 = np.float64(source[batch, y0c, x1c, channel])
                    v10 = np.float64(source[batch, y1c, x0c, channel])
                    v11 = np.float64(source[batch, y1c, x1c, channel])
                    accumulator = np.float64(0.0)
                    accumulator = np.float64(
                        accumulator + v00 * one_minus_y * one_minus_x
                    )
                    accumulator = np.float64(
                        accumulator + v01 * one_minus_y * wx_w
                    )
                    accumulator = np.float64(
                        accumulator + v10 * wy_w * one_minus_x
                    )
                    accumulator = np.float64(accumulator + v11 * wy_w * wx_w)
                    output[batch, oh, ow, channel] = accumulator
    return output


def reference_bicubic(
    job: NormalizedJob,
    input_array: np.ndarray,
    *,
    weight_dtype: np.dtype | type = np.float64,
) -> np.ndarray:
    assert job.scale is not None
    source = _zero_pad(input_array, job.padding)
    n, out_h, out_w, channels = job.output_shape
    padded_h, padded_w = source.shape[1], source.shape[2]
    output = np.empty(job.output_shape, dtype=np.float64)
    scale_h, scale_w = job.scale
    offsets = (-1, 0, 1, 2)
    for batch in range(n):
        for oh in range(out_h):
            source_y = _align_corners_false(oh, scale_h)
            y_base = math.floor(source_y)
            fy = source_y - y_base
            y_indices = [
                _clamped(y_base + offset, 0, padded_h - 1) for offset in offsets
            ]
            wy = [weight_dtype(_cubic_weight(fy - offset)) for offset in offsets]
            for ow in range(out_w):
                source_x = _align_corners_false(ow, scale_w)
                x_base = math.floor(source_x)
                fx = source_x - x_base
                x_indices = [
                    _clamped(x_base + offset, 0, padded_w - 1)
                    for offset in offsets
                ]
                wx = [
                    weight_dtype(_cubic_weight(fx - offset)) for offset in offsets
                ]
                for channel in range(channels):
                    accumulator = np.float64(0.0)
                    for row in range(4):
                        for column in range(4):
                            value = np.float64(
                                source[
                                    batch,
                                    y_indices[row],
                                    x_indices[column],
                                    channel,
                                ]
                            )
                            accumulator = np.float64(
                                accumulator
                                + value * wy[row] * wx[column]
                            )
                    output[batch, oh, ow, channel] = accumulator
    return output


def reference_interpolate(
    job: NormalizedJob, input_array: np.ndarray
) -> np.ndarray:
    if job.interp_mode == "nearest":
        return reference_nearest(job, input_array)
    if job.interp_mode == "bilinear":
        return reference_bilinear(job, input_array)
    if job.interp_mode == "bicubic":
        return reference_bicubic(job, input_array)
    raise ValueError(f"unsupported interpolation mode: {job.interp_mode}")


def reference_output(
    job: NormalizedJob,
    input_array: np.ndarray,
    weight_array: np.ndarray | None,
) -> np.ndarray:
    if job.family == "conv":
        assert weight_array is not None
        return reference_conv2d(job, input_array, weight_array)
    return reference_interpolate(job, input_array)
