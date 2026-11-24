import numpy as np

from matgen.channels import build_channels
from matgen.tiling import chunk_index_at, compare_arrays, plan_chunks, render_tile, stitch
from tests.helpers import make_spec


def _assert_consistent(spec):
    reference = build_channels(spec)
    plans = plan_chunks(spec)
    tiles = [render_tile(spec, plan) for plan in plans]
    composite = stitch(tiles, spec, dtype=np.float64)
    assert set(composite) == set(reference)
    for name in composite:
        passed, mismatch, max_error = compare_arrays(reference[name], composite[name], spec.tolerance)
        assert passed, f"{name} mismatch {mismatch} err {max_error}"
        assert max_error <= 1e-14


def test_consistency_various_grids():
    _assert_consistent(make_spec("a", width=32, height=24, grid_rows=2, grid_cols=2))
    _assert_consistent(make_spec("b", width=48, height=48, grid_rows=3, grid_cols=3))
    _assert_consistent(make_spec("c", width=64, height=32, grid_rows=2, grid_cols=4))


def test_weights_are_positive_and_partition_after_normalization():
    spec = make_spec(width=32, height=24, grid_rows=2, grid_cols=2)
    plans = plan_chunks(spec)
    accum = np.zeros((spec.height, spec.width))
    for plan in plans:
        tile = render_tile(spec, plan)
        assert (tile["weights"] > 0).all()
        accum[plan.y0 : plan.y0 + plan.tile_h, plan.x0 : plan.x0 + plan.tile_w] += tile["weights"]
    # 每个像素至少被一个块覆盖，且非核心区被多个块覆盖（权重和大于 1）。
    assert (accum > 0).all()
    core_w, core_h = spec.width // spec.grid_cols, spec.height // spec.grid_rows
    assert np.isclose(accum[0, 0], 1.0)  # 角块核心起点仅单块覆盖
    assert accum[core_h, core_w] > 1.0  # 四块交汇处多块覆盖


def test_mismatch_reports_first_pixel_and_chunk():
    spec = make_spec(width=32, height=24)
    reference = build_channels(spec)
    plans = plan_chunks(spec)
    tiles = [render_tile(spec, plan) for plan in plans]
    # 人为破坏一个分块，制造超容差差异。
    tiles[2]["channels"]["roughness"][:] += 5.0
    composite = stitch(tiles, spec, dtype=np.float64)
    passed, mismatch, max_error = compare_arrays(reference["roughness"], composite["roughness"], spec.tolerance)
    assert not passed and max_error > 1.0
    assert chunk_index_at(spec, mismatch[0], mismatch[1]) == 2
