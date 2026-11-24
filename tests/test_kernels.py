from __future__ import annotations

import numpy as np

from voice_repair.kernels import (
    DenoiseModel,
    apply_normalization,
    declick_full,
    declick_interval,
    denoise_full,
    normalization_gain,
    remove_dc,
)
from voice_repair.task import (
    DCParams,
    DeclickParams,
    DenoiseParams,
    NormalizeParams,
)


def _ranges(total: int, block: int) -> list[tuple[int, int]]:
    return [(start, min(total, start + block)) for start in range(0, total, block)]


def test_all_stages_are_exactly_chunk_invariant() -> None:
    rng = np.random.default_rng(314159)
    count = 11_237
    values = rng.normal(0.0, 0.2, count)
    values[300] = 1.4
    values[7_100] = -1.3
    dc = DCParams(block_frames=1, mode="mean", offset=0.0)
    declick = DeclickParams(block_frames=1, threshold=0.2, radius=3)
    denoise = DenoiseParams(
        block_frames=1,
        frame_size=256,
        hop_size=64,
        noise_frames=7,
        oversubtraction=1.2,
        floor_ratio=0.02,
    )
    normalize = NormalizeParams(block_frames=1, target_peak=0.88)
    mean = float(np.mean(values))

    dc_full = remove_dc(values, dc, mean)
    dc_blocks = []
    for start, end in _ranges(count, 997):
        dc_blocks.append(remove_dc(values[start:end], dc, mean))
    assert np.array_equal(np.concatenate(dc_blocks), dc_full)

    declick_expected = declick_full(dc_full, declick)
    declick_blocks = []
    for start, end in _ranges(count, 1_013):
        declick_blocks.append(
            declick_interval(
                lambda left, right: dc_full[left:right],
                count,
                start,
                end,
                declick,
            )
        )
    assert np.array_equal(np.concatenate(declick_blocks), declick_expected)

    model = DenoiseModel.build(count, denoise)
    noise = model.noise_profile(lambda start, end: declick_expected[start:end])
    denoise_expected = denoise_full(declick_expected, denoise)
    for block in (777, 1_024, 4_096):
        denoise_blocks = []
        for start, end in _ranges(count, block):
            denoise_blocks.append(
                model.denoise_interval(
                    lambda left, right: declick_expected[left:right],
                    noise,
                    start,
                    end,
                )
            )
        assert np.array_equal(np.concatenate(denoise_blocks), denoise_expected)

    gain = normalization_gain(denoise_expected, normalize)
    normalized = apply_normalization(denoise_expected, gain)
    normalized_blocks = []
    for start, end in _ranges(count, 1_001):
        normalized_blocks.append(
            apply_normalization(denoise_expected[start:end], gain)
        )
    assert np.array_equal(np.concatenate(normalized_blocks), normalized)


def test_declick_handles_true_file_edges() -> None:
    values = np.zeros(20, dtype=np.float64)
    values[0] = 0.9
    values[-1] = -0.8
    params = DeclickParams(block_frames=16, threshold=0.25, radius=2)
    expected = declick_full(values, params)
    actual = np.concatenate(
        [
            declick_interval(
                lambda left, right: values[left:right],
                values.size,
                start,
                end,
                params,
            )
            for start, end in _ranges(values.size, 7)
        ]
    )
    assert np.array_equal(actual, expected)
