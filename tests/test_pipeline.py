from __future__ import annotations

import numpy as np

from matforge.params import MaterialParams
from matforge.pipeline import (
    compute_normalization,
    generate_block,
    generate_full,
    stitch_blocks,
)
from matforge.plan import BlockPlan
from matforge.presets import get_preset, preset_names


def test_full_generation_is_byte_deterministic() -> None:
    params = MaterialParams(seed=918273)
    plan = BlockPlan(width=96, height=64, tile_width=32, tile_height=32)
    first = generate_full(params, plan)
    second = generate_full(params, plan)
    assert first.digest == second.digest
    assert np.array_equal(first.channels, second.channels)
    assert np.array_equal(first.height, second.height)
    assert first.stats == second.stats
    assert [stage.name for stage in first.stages] == [
        "validate+normalization",
        "multi_octave_noise",
        "material_channel_map",
        "block_grid_slice",
        "seam_normalize+stitch",
    ]


def test_blocks_are_global_phase_slices_and_stitch_exactly() -> None:
    params = MaterialParams(seed=77123, seam_width=7.0)
    plan = BlockPlan(width=100, height=73, tile_width=31, tile_height=29)
    stats = compute_normalization(params, plan)
    full = generate_full(params, plan)
    blocks: dict[int, np.ndarray] = {}
    for block in plan.blocks():
        texture = generate_block(params, plan, block.index, stats)
        blocks[block.index] = texture.channels
        assert texture.channels.shape == (block.height, block.width, 7)
        assert texture.block.pixel_range == {"x0": block.x0, "y0": block.y0, "x1": block.x1, "y1": block.y1}
        assert np.array_equal(texture.channels, full.channels[block.y0 : block.y1, block.x0 : block.x1])
    stitched = stitch_blocks(plan, blocks)
    assert stitched.tobytes() == full.channels.tobytes()
    assert np.array_equal(stitched, full.channels)


def test_seed_and_phase_change_output_but_same_inputs_do_not() -> None:
    plan = BlockPlan(width=64, height=64, tile_width=32, tile_height=32)
    a = generate_full(MaterialParams(seed=1), plan)
    b = generate_full(MaterialParams(seed=2), plan)
    c = generate_full(MaterialParams(seed=1, noise=MaterialParams().noise.__class__(phase_x=0.25)), plan)
    assert a.digest != b.digest
    assert a.digest != c.digest
    assert a.digest == generate_full(MaterialParams(seed=1), plan).digest


def test_fixed_seed_material_presets_are_distinct_and_repeatable() -> None:
    plan = BlockPlan(width=48, height=32, tile_width=24, tile_height=16)
    digests = set()
    for name in preset_names():
        first = generate_full(get_preset(name), plan)
        second = generate_full(get_preset(name), plan)
        assert first.digest == second.digest
        digests.add(first.digest)
    assert len(digests) == len(preset_names())

