"""Quantization primitives and the blocked fixed-point conv core."""
import numpy as np

from quant_conv_pipeline import conv_core
from quant_conv_pipeline.quant import dequantize, quantize_int8
from quant_conv_pipeline.sample_gen import (default_jobs, gen_activation,
                                            gen_kernel)


def test_quantize_dequantize_roundtrip_bounded():
    rng = np.random.default_rng(7)
    x = rng.standard_normal((2, 3, 8, 8))
    scale = float(np.max(np.abs(x)) / 120.0)
    q = quantize_int8(x, scale, -2)
    assert q.dtype == np.int8
    err = np.abs(dequantize(q, scale, -2) - x)
    assert err.max() <= scale / 2 + 1e-12


def test_quantize_clips_to_int8():
    x = np.array([[-1000.0, 0.0, 1000.0]])
    q = quantize_int8(x, 1.0, 0)
    assert q.min() == -128 and q.max() == 127


def test_blocked_mac_exact_vs_int64_for_all_block_sizes():
    rng = np.random.default_rng(0)
    x = rng.standard_normal((2, 3, 6, 6))
    w = rng.standard_normal((4, 3, 3, 3))
    sa, za, sw, zw = 0.02, -1, 0.003, 2
    qa, qw = quantize_int8(x, sa, za), quantize_int8(w, sw, zw)
    cols = conv_core.im2col(qa, 3, 3, 1, 1, pad_value=za)
    wmat = conv_core.kernel_matrix(qw)
    cols64 = conv_core.im2col(qa.astype(np.int64) - za, 3, 3, 1, 1)
    exact = cols64 @ conv_core.kernel_matrix(qw.astype(np.int64) - zw)
    for bs in (1, 3, 7, 27, 256):
        acc = conv_core.blocked_int8_mac(cols, wmat, bs, za, zw)
        assert acc.dtype == np.int32
        assert np.array_equal(acc.astype(np.int64), exact), f"bs={bs}"


def test_zero_point_compensation_matches_shifted_semantics():
    rng = np.random.default_rng(3)
    x = rng.standard_normal((1, 2, 5, 5))
    w = rng.standard_normal((3, 2, 3, 3))
    sa, za, sw, zw = 0.05, 3, 0.01, -4
    qa, qw = quantize_int8(x, sa, za), quantize_int8(w, sw, zw)
    cols = conv_core.im2col(qa, 3, 3, 1, 1, pad_value=za)
    acc = conv_core.blocked_int8_mac(cols, conv_core.kernel_matrix(qw),
                                     4, za, zw)
    ref = conv_core.float_reference_conv(dequantize(qa, sa, za),
                                         dequantize(qw, sw, zw), 1, 1)
    ref_flat = ref.transpose(0, 2, 3, 1).reshape(acc.shape)
    assert np.abs(acc * sa * sw - ref_flat).max() < 1e-9


def test_determinism_byte_identical():
    job = default_jobs(3)[1]
    a1, a2 = gen_activation(job), gen_activation(job)
    k1, k2 = gen_kernel(job), gen_kernel(job)
    assert a1.tobytes() == a2.tobytes()
    assert k1.tobytes() == k2.tobytes()
    q1 = quantize_int8(a1, job["quant_scale"], job["zero_point"])
    q2 = quantize_int8(a2, job["quant_scale"], job["zero_point"])
    assert q1.tobytes() == q2.tobytes()
