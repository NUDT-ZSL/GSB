"""Low-precision blocked convolution core.

Layout conventions:
    activations: (N, C, H, W) float / int8
    kernels:     (K, C, Kh, Kw) float / int8
    output:      (N, K, OH, OW)

The fixed-point path quantizes both operands to int8, runs blocked
int8 x int8 -> int32 multiply-accumulate over the reduction dimension
(C*Kh*Kw) with zero-point (offset) compensation per block, and reduces
the per-block int32 partial sums into a single int32 accumulator.
The float64 dequantized path of the same operands is the reference.
"""
from __future__ import annotations

import numpy as np


def out_hw(h, w, kh, kw, stride, padding):
    oh = (h + 2 * padding - kh) // stride + 1
    ow = (w + 2 * padding - kw) // stride + 1
    return oh, ow


def im2col(x: np.ndarray, kh: int, kw: int, stride: int, padding: int,
           pad_value=0) -> np.ndarray:
    """(N,C,H,W) -> (N*OH*OW, C*Kh*Kw) columns, deterministic order.

    ``pad_value`` is the padded element value: 0.0 for float tensors,
    the quantization zero_point for quantized tensors (real 0.0 maps to
    zero_point after affine quantization).
    """
    n, c, h, w = x.shape
    oh, ow = out_hw(h, w, kh, kw, stride, padding)
    xp = np.pad(x, ((0, 0), (0, 0), (padding, padding), (padding, padding)),
                constant_values=pad_value)
    cols = np.empty((n, oh, ow, c, kh, kw), dtype=x.dtype)
    for i in range(kh):
        for j in range(kw):
            cols[:, :, :, :, i, j] = \
                xp[:, :, i:i + (oh - 1) * stride + 1:stride,
                     j:j + (ow - 1) * stride + 1:stride].transpose(0, 2, 3, 1)
    return cols.reshape(n * oh * ow, c * kh * kw)


def kernel_matrix(w: np.ndarray) -> np.ndarray:
    """(K,C,Kh,Kw) -> (C*Kh*Kw, K)."""
    k, c, kh, kw = w.shape
    return w.transpose(1, 2, 3, 0).reshape(c * kh * kw, k)


def blocked_int8_mac(q_cols: np.ndarray, q_wmat: np.ndarray, block_size: int,
                     act_zero_point: int, wgt_zero_point: int) -> np.ndarray:
    """Blocked int8 MAC with int32 accumulators and offset compensation.

    Computes, over the reduction axis split into blocks of ``block_size``:
        acc += sum_block( (qa - za) * (qw - zw) )
    expanded per block as int32 arithmetic:
        sum(qa*qw) - zw*sum(qa) - za*sum(qw) + n_block*za*zw
    Returns the (M, K) int32 accumulator.
    """
    m, red = q_cols.shape
    k = q_wmat.shape[1]
    za, zw = int(act_zero_point), int(wgt_zero_point)
    acc = np.zeros((m, k), dtype=np.int32)
    for start in range(0, red, block_size):
        stop = min(start + block_size, red)
        a_blk = q_cols[:, start:stop].astype(np.int32)
        w_blk = q_wmat[start:stop, :].astype(np.int32)
        n_blk = stop - start
        partial = a_blk @ w_blk
        if zw:
            partial = partial - zw * a_blk.sum(axis=1, keepdims=True)
        if za:
            partial = partial - za * w_blk.sum(axis=0, keepdims=True)
        if za and zw:
            partial = partial + n_blk * za * zw
        acc = acc + partial.astype(np.int32)
    return acc


def dequant_output(acc: np.ndarray, act_scale: float, wgt_scale: float,
                   out_scale: float, out_zero_point: int) -> np.ndarray:
    """Reduce the int32 accumulator and requantize to int8 output."""
    from .quant import quantize_int8
    f = acc.astype(np.float64) * float(act_scale) * float(wgt_scale)
    return quantize_int8(f, out_scale, out_zero_point)


def float_reference_conv(x: np.ndarray, w: np.ndarray, stride: int,
                         padding: int) -> np.ndarray:
    """float64 ground-truth convolution, (N,C,H,W)x(K,C,Kh,Kw)->(N,K,OH,OW)."""
    n, c, h, wdt = x.shape
    k, _, kh, kw = w.shape
    oh, ow = out_hw(h, wdt, kh, kw, stride, padding)
    cols = im2col(x.astype(np.float64), kh, kw, stride, padding)
    wmat = kernel_matrix(w.astype(np.float64))
    out = cols @ wmat
    return out.reshape(n, oh, ow, k).transpose(0, 3, 1, 2)


def estimate_block_resident_bytes(block_size: int, rows: int, k: int) -> int:
    """Resident estimate of one blocked MAC tile (int32 operands)."""
    return int(block_size) * (int(rows) + int(k)) * np.dtype(np.int32).itemsize
