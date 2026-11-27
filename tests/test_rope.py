from __future__ import annotations

import math

import pytest

from georope.normalize import normalize_request
from georope.rope import encode_block


def _single_block_payload() -> dict:
    return {
        "tolerance": 1e-12,
        "phase_threshold": 1e-9,
        "pixel_anchor": "corner",
        "blocks": [
            {
                "schema_version": "v1",
                "block_id": "rope-formula-block",
                "tile_set_id": "rope-formula",
                "tile_key": "cell",
                "origin_x": 4.0,
                "origin_y": 0.0,
                "origin_unit": "meters",
                "pixel_size": [2.0, 2.0],
                "shape": [1, 1, 4],
                "features": [[[1.0, 0.0, 1.0, 0.0]]],
                "channel_ranges": [
                    {"name": "formula_band", "channel_range": [0, 4]}
                ],
                "frequency_bands": [
                    {
                        "name": "formula_band",
                        "angle_base": 100.0,
                        "frequency_scale": 0.5,
                        "frequency_axis": [1.0, 0.0],
                    }
                ],
            }
        ],
        "band_combinations": [{"name": "formula", "bands": ["formula_band"]}],
    }


def test_rope_angle_base_frequency_scale_and_pair_boundaries() -> None:
    request = normalize_request(_single_block_payload())
    encoding = encode_block(request.blocks[0], request.combinations[0])
    expected_pair_0 = 2.0
    expected_pair_1 = 0.5 * 4.0 * (100.0 ** -0.5)
    assert encoding.angles[0, 0] == pytest.approx(expected_pair_0)
    assert encoding.angles[0, 1] == pytest.approx(expected_pair_0)
    assert encoding.angles[0, 2] == pytest.approx(expected_pair_1)
    assert encoding.angles[0, 3] == pytest.approx(expected_pair_1)
    values = encoding.encoded.reshape(-1, 4)[0]
    assert values[0] == pytest.approx(math.cos(expected_pair_0))
    assert values[1] == pytest.approx(math.sin(expected_pair_0))
    assert values[2] == pytest.approx(math.cos(expected_pair_1))
    assert values[3] == pytest.approx(math.sin(expected_pair_1))


def test_geographic_position_uses_global_not_local_coordinate() -> None:
    request = normalize_request(_single_block_payload())
    block = request.blocks[0]
    x, y = block.positions()
    assert x.tolist() == [[4.0]]
    assert y.tolist() == [[0.0]]
