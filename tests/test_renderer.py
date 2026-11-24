from __future__ import annotations

import numpy as np

from relief_workbench.pattern import PatternSynthesizer
from relief_workbench.renderer import ReliefRenderer


def test_height_repeatable_and_coordinate_local(small_config) -> None:
    synth = PatternSynthesizer(small_config)
    full = synth.height_patch(0, 64, 0, 48)
    repeated = synth.height_patch(0, 64, 0, 48)
    sub = synth.height_patch(11, 37, 7, 29)
    assert np.array_equal(full, repeated)
    assert np.array_equal(full[7:29, 11:37], sub)


def test_seed_changes_height(small_config) -> None:
    from dataclasses import replace

    first = PatternSynthesizer(small_config).height_patch(0, 32, 0, 32)
    second = PatternSynthesizer(
        replace(small_config, seed=small_config.seed + 1)
    ).height_patch(0, 32, 0, 32)
    assert not np.array_equal(first, second)


def test_tile_raw_light_uses_global_neighbors(small_config) -> None:
    renderer = ReliefRenderer(small_config)
    stats = renderer.compute_global_stats()
    full_raw = renderer.raw_light_patch(
        0, small_config.width, 0, small_config.height
    )
    for spec in renderer.specs:
        tile_raw = renderer.raw_light_patch(
            spec.origin_x, spec.end_x, spec.origin_y, spec.end_y
        )
        assert np.array_equal(
            tile_raw,
            full_raw[spec.origin_y : spec.end_y, spec.origin_x : spec.end_x],
        )

    spec = renderer.spec(1, 2)
    tile_raw = renderer.raw_light_patch(
        spec.origin_x, spec.end_x, spec.origin_y, spec.end_y
    )
    local_raw = renderer.raw_light_local_gradient(spec)
    assert np.any(np.abs(tile_raw[:, 0] - local_raw[:, 0]) > 1e-12)
    tile_gray = renderer.render_tile(spec, stats)
    full_gray = renderer.render_full(stats)
    assert np.array_equal(
        tile_gray,
        full_gray[spec.origin_y : spec.end_y, spec.origin_x : spec.end_x],
    )


def test_global_normalization_uses_full_canvas(small_config) -> None:
    renderer = ReliefRenderer(small_config)
    stats = renderer.compute_global_stats()
    full_raw = renderer.raw_light_patch(
        0, small_config.width, 0, small_config.height
    )
    assert stats.minimum == float(np.min(full_raw))
    assert stats.maximum == float(np.max(full_raw))
    grayscale = renderer.render_full(stats)
    assert grayscale.dtype == np.uint8
    assert int(grayscale.min()) == 0
    assert int(grayscale.max()) == 255
