import pytest

from contour_tool.contour import extract_all
from contour_tool.errors import SceneError
from contour_tool.scene import load_scene


RAW_NAN = """{
  "grid": {"width": 2, "height": 2, "values": [0, 10, NaN, 0]},
  "levels": [5],
  "queries": [],
  "canvas": {"width": 100, "height": 100}
}"""

RAW_INF = """{
  "grid": {"width": 2, "height": 2, "values": [0, Infinity, 10, 0]},
  "levels": [5],
  "queries": [{"x": 1, "y": 1}],
  "canvas": {"width": 100, "height": 100}
}"""


def test_nan_literal_in_values_is_treated_as_gap(tmp_path):
    path = tmp_path / "nan.json"
    path.write_text(RAW_NAN, encoding="utf-8")
    loaded = load_scene(str(path))
    assert loaded.scene.grid.values == (0.0, 10.0, None, 0.0)
    assert loaded.scene.data_max == 10.0
    assert loaded.affected_levels == (5.0,)
    assert any("无效值" in w for w in loaded.warnings)


def test_infinity_literal_in_values_is_treated_as_gap(tmp_path):
    path = tmp_path / "inf.json"
    path.write_text(RAW_INF, encoding="utf-8")
    loaded = load_scene(str(path))
    assert loaded.scene.grid.values == (0.0, None, 10.0, 0.0)
    # The single cell touches the gap, so level 5 is reported as affected and
    # extraction legitimately yields no segments.
    assert loaded.affected_levels == (5.0,)
    assert not extract_all(loaded.scene)[5.0].segments


def test_nan_level_is_rejected():
    from conftest import scene_dict
    from contour_tool.scene import parse_scene

    data = scene_dict(2, 1, [0, 10], [])
    data["levels"] = [float("nan")]
    with pytest.raises(SceneError, match="有限数字"):
        parse_scene(data)


def test_non_finite_world_rejected():
    from conftest import scene_dict
    from contour_tool.scene import parse_scene

    data = scene_dict(2, 1, [0, 10], [5],
                      world={"min_x": 0, "min_y": 0, "max_x": 1e400, "max_y": 1})
    # 1e400 parses as inf via float(); JSON text never produces it, but a
    # programmatic caller can, and must still get a clean error.
    with pytest.raises(SceneError, match="world.max_x"):
        parse_scene(data)
