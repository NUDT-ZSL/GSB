import json

import pytest
from PIL import Image

from conftest import scene_dict, write_scene
from contour_tool.cli import run


def run_scene(tmp_path, data):
    scene_path = write_scene(tmp_path, data)
    png = tmp_path / "out" / "preview.png"
    res = tmp_path / "out" / "result.json"
    assert run(str(scene_path), str(png), str(res)) == 0
    return png, json.loads(res.read_text(encoding="utf-8"))


def test_report_lists_levels_sorted_and_deduplicated(tmp_path):
    data = scene_dict(2, 2, [0, 10, 10, 0], [0.5, 5, 0.5, 2.5])
    _, report = run_scene(tmp_path, data)
    assert [item["level"] for item in report["levels"]] == [0.5, 2.5, 5.0]
    assert len(report["levels"]) == 3


def test_empty_level_is_marked_not_raised(tmp_path):
    data = scene_dict(2, 2, [0, 5, 5, 10], [15, 5])
    _, report = run_scene(tmp_path, data)
    by_level = {item["level"]: item for item in report["levels"]}
    assert by_level[15]["empty"] is True
    assert by_level[15]["segment_count"] == 0
    assert by_level[15]["in_data_range"] is False
    assert by_level[5]["empty"] is False


def test_affected_levels_reported_for_gap(tmp_path):
    # Gap at (1,1).  It shares a cell with {0, 10, 0} -> levels 0-10 are
    # affected there, and with {10, 20, 0} -> 0-20 as well.
    values = [0.0, 10.0, 20.0, 0.0, None, 0.0]
    data = scene_dict(3, 2, values, [5, 15])
    _, report = run_scene(tmp_path, data)
    assert report["affected_levels"] == [5.0, 15.0]
    assert report["warnings"]


def test_affected_levels_ignores_levels_outside_gap_cells(tmp_path):
    values = [0.0, 20.0, 30.0, 0.0, None, 1.0]
    data = scene_dict(3, 2, values, [25, 40])
    _, report = run_scene(tmp_path, data)
    assert report["affected_levels"] == [25.0]


def test_gap_still_produces_other_levels(tmp_path):
    # 4x2 grid; the NaN sits in the left half, the right half is clean.
    values = [0.0, 10.0, 20.0, 30.0,
              0.0, float("nan"), 20.0, 30.0]
    data = scene_dict(4, 2, values, [5, 25])
    png, report = run_scene(tmp_path, data)
    assert png.is_file()
    by_level = {item["level"]: item for item in report["levels"]}
    assert by_level[25.0]["segment_count"] > 0


def test_query_payload_fields(tmp_path):
    data = scene_dict(
        2, 2, [0, 10, 10, 0], [5],
        world={"min_x": 0, "min_y": 0, "max_x": 2, "max_y": 2},
        queries=[{"x": 1, "y": 0, "label": "命中"}],
    )
    _, report = run_scene(tmp_path, data)
    hit = report["queries"][0]
    assert hit["x"] == 1 and hit["y"] == 0 and hit["label"] == "命中"
    assert hit["status"] == "hit"
    assert hit["level"] == 5.0
    assert hit["nearest"] == pytest.approx([1.0, 0.0])
    assert hit["distance"] == pytest.approx(0.0, abs=1e-9)
    assert hit["pixel"] is not None


def test_miss_nearest_is_null_not_zero(tmp_path):
    data = scene_dict(2, 2, [0, 10, 10, 0], [99], queries=[{"x": 1, "y": 1}])
    _, report = run_scene(tmp_path, data)
    entry = report["queries"][0]
    assert entry["nearest"] is None
    assert entry["distance"] is None
    assert entry["level"] is None
    assert entry["status"] == "miss"


def test_result_json_is_utf8_and_parseable(tmp_path):
    data = scene_dict(2, 2, [0, 10, 10, 0], [5], queries=[{"x": 1, "y": 0, "label": "中文标签"}])
    res = tmp_path / "out" / "result.json"
    out_png = tmp_path / "out" / "preview.png"
    scene_path = write_scene(tmp_path, data)
    assert run(str(scene_path), str(out_png), str(res)) == 0
    raw = res.read_bytes()
    assert "中文标签".encode("utf-8") in raw
    json.loads(raw.decode("utf-8"))


def test_query_pixel_matches_drawn_pixel(tmp_path):
    """The pixel recorded in the JSON must land on the drawn line (<=1 px)."""
    data = scene_dict(
        4, 4,
        [0, 3, 6, 9,
         1, 4, 7, 10,
         2, 5, 8, 11,
         3, 6, 9, 12],
        [5, 8],
        world={"min_x": 0, "min_y": 0, "max_x": 300, "max_y": 300},
        canvas=(400, 400),
        queries=[{"x": 120, "y": 160}],
    )
    png, report = run_scene(tmp_path, data)
    entry = report["queries"][0]
    assert entry["status"] == "hit"
    px, py = entry["pixel"]
    with Image.open(png) as image:
        image = image.convert("RGB")
        found = False
        for dx in range(-1, 2):
            for dy in range(-1, 2):
                x, y = int(round(px)) + dx, int(round(py)) + dy
                if 0 <= x < image.width and 0 <= y < image.height:
                    if image.getpixel((x, y)) != (255, 255, 255):
                        found = True
        assert found, f"no contour pixel within 1 px of {entry['pixel']}"


def test_queries_order_preserved_with_skips(tmp_path):
    data = scene_dict(
        2, 2, [0, 10, 10, 0], [5],
        world={"min_x": 0, "min_y": 0, "max_x": 2, "max_y": 2},
        queries=[
            {"x": 1, "y": 0},
            {"x": 50, "y": 0},
            {"x": 1, "y": 2},
            {"x": "x", "y": 0},
        ],
    )
    _, report = run_scene(tmp_path, data)
    assert [q["index"] for q in report["queries"]] == [0, 1, 2, 3]
    assert [q["status"] for q in report["queries"]] == ["hit", "skipped", "hit", "skipped"]
    assert report["queries"][3]["x"] is None
    assert "reason" in report["queries"][1]
