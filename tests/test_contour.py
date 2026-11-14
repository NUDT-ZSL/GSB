import math

import pytest

from conftest import scene_dict
from contour_tool.contour import extract_all, extract_level, locate
from contour_tool.scene import parse_scene


def make_scene(*args, **kwargs):
    return parse_scene(scene_dict(*args, **kwargs)).scene


def test_midpoint_crossing_on_two_cells():
    # One row, two samples 0 and 10, level 5 -> crossing at the edge midpoint.
    scene = make_scene(2, 1, [0, 10], [5], world={"min_x": 0, "min_y": 0, "max_x": 10, "max_y": 1})
    contour = extract_level(scene, 5)
    assert len(contour.segments) == 1
    a, b = contour.segments[0]
    assert a == pytest.approx((5.0, 0.0))
    assert b == pytest.approx((5.0, 0.0))


def test_midpoint_inside_single_cell():
    # One cell with row-major corners [0, 0, 10, 10] in a 2x2 world:
    # level 5 crosses the left and right cell edges, each at its midpoint.
    scene = make_scene(
        2, 2, [0, 0, 10, 10], [5],
        world={"min_x": 0, "min_y": 0, "max_x": 2, "max_y": 2},
    )
    contour = extract_level(scene, 5)
    assert len(contour.segments) == 1
    a, b = contour.segments[0]
    assert {tuple(round(v, 9) for v in a), tuple(round(v, 9) for v in b)} == {
        (0.0, 1.0),
        (2.0, 1.0),
    }


def test_saddle_cell_pairs_crossings_into_two_segments():
    # Diagonal 0 / 10 corners: four edge midpoints, resolved by the centre value.
    scene = make_scene(
        2, 2, [0, 10, 10, 0], [5],
        world={"min_x": 0, "min_y": 0, "max_x": 2, "max_y": 2},
    )
    contour = extract_level(scene, 5)
    assert len(contour.segments) == 2
    found = {
        tuple(round(v, 9) for v in point)
        for segment in contour.segments
        for point in segment
    }
    assert found == {(0.0, 1.0), (2.0, 1.0), (1.0, 0.0), (1.0, 2.0)}


def test_no_segment_when_level_not_crossed():
    scene = make_scene(2, 1, [0, 10], [15])
    assert extract_level(scene, 15).empty


def test_linear_interpolation_position():
    scene = make_scene(
        2, 1, [0, 8], [2],
        world={"min_x": 0, "min_y": 0, "max_x": 8, "max_y": 1},
    )
    a, b = extract_level(scene, 2).segments[0]
    assert a[0] == pytest.approx(2.0)


def test_levels_sorted_and_deduplicated():
    scene = make_scene(2, 2, [0, 10, 10, 0], [0.5, 5, 0.5, 2.5])
    assert scene.levels == (0.5, 2.5, 5.0)


def test_duplicate_fractional_level_yields_single_entry():
    scene = make_scene(2, 2, [0, 1, 1, 0], [0.5, 0.5])
    contours = extract_all(scene)
    assert list(contours) == [0.5]


def test_nan_cell_skips_touching_cells_but_keeps_others():
    # 3x2 grid; cell (0,0)-(1,1) touches NaN, right column stays usable.
    values = [float("nan"), 10.0, 20.0,
              0.0, 0.0, 0.0]
    scene = make_scene(
        3, 2, values, [5],
        world={"min_x": 0, "min_y": 0, "max_x": 2, "max_y": 1},
    )
    contours = extract_all(scene)
    segs = contours[5].segments
    # Only the right-hand cell contributes.
    assert segs
    for a, b in segs:
        assert min(a[0], b[0]) >= 1.0


def test_infinite_value_behaves_like_gap():
    values = [0.0, float("inf"), 4.0,
              0.0, 10.0, 4.0]
    scene = make_scene(3, 2, values, [5])
    assert extract_level(scene, 5).segments == ()


def test_level_outside_data_range_is_empty_not_error():
    scene = make_scene(2, 2, [0, 5, 5, 10], [15])
    contours = extract_all(scene)
    assert contours[15].empty


def test_query_on_segment_has_near_zero_distance():
    scene = make_scene(
        2, 2, [0, 10, 10, 0], [5],
        world={"min_x": 0, "min_y": 0, "max_x": 2, "max_y": 2},
        queries=[{"x": 1, "y": 0}],
    )
    contours = extract_all(scene)
    hit = locate(scene, contours)[0]
    assert hit.status == "hit"
    assert hit.level == 5
    assert hit.distance == pytest.approx(0.0, abs=1e-9)
    assert hit.nearest == pytest.approx((1.0, 0.0))


def test_query_result_count_matches_query_count():
    scene = make_scene(
        2, 2, [0, 10, 10, 0], [5],
        world={"min_x": 0, "min_y": 0, "max_x": 2, "max_y": 2},
        queries=[{"x": 0, "y": 0}, {"x": 1, "y": 1}, {"x": 2, "y": 2}],
    )
    assert len(locate(scene, extract_all(scene))) == 3


def test_out_of_bounds_query_is_skipped_with_reason():
    scene = make_scene(
        2, 2, [0, 10, 10, 0], [5],
        world={"min_x": 0, "min_y": 0, "max_x": 2, "max_y": 2},
        queries=[{"x": 99, "y": 0}, {"x": 1, "y": 0}],
    )
    hits = locate(scene, extract_all(scene))
    assert hits[0].status == "skipped"
    assert "超出世界范围" in hits[0].reason
    assert hits[0].nearest is None
    assert hits[1].status == "hit"
    assert hits[1].level == 5


def test_hit_outranks_skipped_and_order_is_preserved():
    scene = make_scene(
        2, 2, [0, 10, 10, 0], [5],
        world={"min_x": 0, "min_y": 0, "max_x": 2, "max_y": 2},
        queries=[{"x": 1, "y": 0}, {"x": -1, "y": 0}, {"x": 1, "y": 2}],
    )
    hits = locate(scene, extract_all(scene))
    assert [h.status for h in hits] == ["hit", "skipped", "hit"]


def test_miss_has_null_nearest():
    scene = make_scene(
        2, 2, [0, 10, 10, 0], [99], queries=[{"x": 1, "y": 0}]
    )
    hit = locate(scene, extract_all(scene))[0]
    assert hit.status == "miss"
    assert hit.nearest is None
    assert hit.level is None


def test_world_scale_affects_distance_not_pixels():
    # Same layout, world stretched 10x: distances scale by 10.
    base = parse_scene(scene_dict(2, 2, [0, 10, 10, 0], [5],
                                  world={"min_x": 0, "min_y": 0, "max_x": 2, "max_y": 2},
                                  queries=[{"x": 1, "y": 1}])).scene
    stretched = parse_scene(scene_dict(2, 2, [0, 10, 10, 0], [5],
                                       world={"min_x": 0, "min_y": 0, "max_x": 20, "max_y": 20},
                                       queries=[{"x": 10, "y": 10}])).scene
    d1 = locate(base, extract_all(base))[0].distance
    d2 = locate(stretched, extract_all(stretched))[0].distance
    assert d2 == pytest.approx(d1 * 10)
