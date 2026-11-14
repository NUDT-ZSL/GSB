import pytest

from conftest import scene_dict, write_scene
from contour_tool.errors import SceneError
from contour_tool.scene import load_scene, parse_scene


def test_valid_3x2_scene_loads(tmp_path):
    path = write_scene(tmp_path, scene_dict(3, 2, [0, 1, 2, 3, 4, 5], [2.5]))
    loaded = load_scene(str(path))
    assert loaded.scene.grid.width == 3
    assert loaded.scene.grid.height == 2
    assert len(loaded.scene.grid.values) == 6
    assert loaded.warnings == []


def test_length_mismatch_is_rejected(tmp_path, writable_out):
    from contour_tool.errors import SceneError as SceneErr

    path = write_scene(tmp_path, scene_dict(3, 2, [0, 1, 2], [1]))
    with pytest.raises(SceneErr, match="标量数组长度不符"):
        load_scene(str(path))
    # The CLI must not produce a preview for an invalid scene.
    from contour_tool.cli import main

    png = writable_out / "bad.png"
    result = main([str(path), "--png", str(png), "--json", str(writable_out / "bad.json")])
    assert result == 2
    assert not png.exists()


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda d: d.pop("grid"), "缺少字段"),
        (lambda d: d["grid"].pop("values"), "缺少字段"),
        (lambda d: d["grid"].__setitem__("width", 0), "width"),
        (lambda d: d["grid"].__setitem__("height", "2"), "height"),
        (lambda d: d["canvas"].__setitem__("width", 0), "canvas.width"),
        (lambda d: d.__setitem__("levels", []), "levels"),
        (lambda d: d.__setitem__("levels", [1, "x"]), "有限数字"),
        (lambda d: d.__setitem__("queries", {"x": 1}), "queries"),
        (lambda d: d.__setitem__("world", {"min_x": 5, "min_y": 0, "max_x": 0, "max_y": 1}), "world"),
        (lambda d: d["grid"]["values"].__setitem__(0, "zero"), "不是数字"),
        (lambda d: d.__setitem__("background", [1, 2]), "长度为 3"),
        (lambda d: d.__setitem__("background", [1, 2, 300]), "0-255"),
        (lambda d: d.__setitem__("line_width", 0), "line_width"),
    ],
)
def test_structural_errors(mutate, message):
    data = scene_dict(2, 1, [0, 10], [5])
    mutate(data)
    with pytest.raises(SceneError, match=message):
        parse_scene(data)


def test_top_level_must_be_object():
    with pytest.raises(SceneError, match="JSON 对象"):
        parse_scene([1, 2, 3])


def test_boolean_coordinate_rejected_but_skipped_not_fatal():
    data = scene_dict(2, 2, [0, 10, 10, 0], [5], queries=[{"x": True, "y": 0}])
    loaded = parse_scene(data)
    assert loaded.scene.queries == (None,)
    assert any("查询点 0" in w for w in loaded.warnings)


def test_default_world_uses_cell_indices():
    loaded = parse_scene(scene_dict(3, 2, [0, 1, 2, 3, 4, 5], [1]))
    world = loaded.scene.world
    assert (world.min_x, world.min_y, world.max_x, world.max_y) == (0, 0, 2, 1)
