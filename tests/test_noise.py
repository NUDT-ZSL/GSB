import numpy as np

from matgen.noise import fbm_field, fbm_point


def test_point_equals_field_cell():
    freqs = (1 / 32, 1 / 16, 1 / 8)
    amps = (0.5, 0.3, 0.2)
    field = fbm_field(8, 6, seed=7, frequencies=freqs, amplitudes=amps)
    for x, y in [(0, 0), (3, 2), (7, 5)]:
        assert fbm_point(x, y, 7, freqs, amps) == field[y, x]


def test_window_independent_of_chunking():
    freqs = (1 / 32, 1 / 16)
    amps = (0.6, 0.4)
    full = fbm_field(32, 24, seed=11, frequencies=freqs, amplitudes=amps)
    window = fbm_field(6, 5, seed=11, frequencies=freqs, amplitudes=amps, x0=20, y0=10)
    assert np.array_equal(window, full[10:15, 20:26])


def test_deterministic_across_calls_and_order():
    freqs = (1 / 32, 1 / 16)
    amps = (0.6, 0.4)
    first = fbm_field(16, 16, seed=3, frequencies=freqs, amplitudes=amps)
    # 插入无关调用，验证结果不依赖调用顺序或全局随机状态。
    fbm_field(16, 16, seed=99, frequencies=freqs, amplitudes=amps)
    second = fbm_field(16, 16, seed=3, frequencies=freqs, amplitudes=amps)
    assert np.array_equal(first, second)


def test_different_seed_diverges():
    freqs = (1 / 32, 1 / 16)
    amps = (0.6, 0.4)
    a = fbm_field(16, 16, seed=1, frequencies=freqs, amplitudes=amps)
    b = fbm_field(16, 16, seed=2, frequencies=freqs, amplitudes=amps)
    assert not np.array_equal(a, b)
