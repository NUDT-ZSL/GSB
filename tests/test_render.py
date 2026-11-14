import json
from pathlib import Path

import pytest
from PIL import Image

from conftest import scene_dict, write_scene
from contour_tool.cli import run
from contour_tool.contour import extract_all
from contour_tool.render import render_preview
from contour_tool.scene import parse_scene


def make_scene(*args, **kwargs):
    return parse_scene(scene_dict(*args, **kwargs)).scene


def test_png_written_with_requested_size(tmp_path):
    scene_path = write_scene(tmp_path, scene_dict(2, 2, [0, 10, 10, 0], [5],
                                                  canvas=(320, 240)))
    png = tmp_path / "out" / "preview.png"
    res = tmp_path / "out" / "result.json"
    assert run(str(scene_path), str(png), str(res)) == 0
    with Image.open(png) as image:
        assert image.size == (320, 240)
        assert image.format == "PNG"


def test_background_fills_undrawn_area(tmp_path):
    scene_path = write_scene(tmp_path, scene_dict(2, 2, [0, 10, 10, 0], [5],
                                                  canvas=(100, 100),
                                                  background=[10, 20, 30]))
    png = tmp_path / "preview.png"
    assert run(str(scene_path), str(png), str(tmp_path / "r.json")) == 0
    with Image.open(png) as image:
        image = image.convert("RGB")
        # Corner is far from the diagonal contour.
        assert image.getpixel((0, 0)) == (10, 20, 30)
        assert image.getpixel((99, 99)) == (10, 20, 30)


def test_levels_have_different_colours(tmp_path):
    scene_path = write_scene(
        tmp_path,
        scene_dict(2, 1, [0, 10], [2.5, 7.5], canvas=(100, 20)),
    )
    png = tmp_path / "preview.png"
    assert run(str(scene_path), str(png), str(tmp_path / "r.json")) == 0
    with Image.open(png) as image:
        image = image.convert("RGB")
        cols = set()
        for x in range(image.width):
            for y in range(image.height):
                px = image.getpixel((x, y))
                if px != (255, 255, 255):
                    cols.add(px)
    assert len(cols) >= 2


def test_preview_pixels_track_the_line(tmp_path):
    scene = make_scene(
        2, 2, [0, 10, 10, 0], [5],
        world={"min_x": 0, "min_y": 0, "max_x": 2, "max_y": 2},
        canvas=(200, 200),
    )
    contours = extract_all(scene)
    image = render_preview(scene, contours)
    for a, b in contours[5].segments:
        for point in (a, b):
            px, py = scene.world_to_pixel(*point)
            # The drawn 2px line must cover the mapped pixel.
            hit = False
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    x, y = int(round(px)) + dx, int(round(py)) + dy
                    if 0 <= x < image.width and 0 <= y < image.height:
                        if image.getpixel((x, y)) != scene.background:
                            hit = True
            assert hit


def test_preview_with_markers_does_not_change_contour_pixels(tmp_path):
    scene_path = write_scene(
        tmp_path,
        scene_dict(2, 2, [0, 10, 10, 0], [5], canvas=(200, 200),
                   world={"min_x": 0, "min_y": 0, "max_x": 2, "max_y": 2},
                   queries=[{"x": 0.5, "y": 0.5}]),
    )
    plain = tmp_path / "plain.png"
    marked = tmp_path / "marked.png"
    assert run(str(scene_path), str(plain), str(tmp_path / "a.json")) == 0
    from contour_tool.cli import main

    assert main([str(scene_path), "--png", str(marked), "--json", str(tmp_path / "b.json"),
                 "--markers"]) == 0
    with Image.open(plain) as a, Image.open(marked) as b:
        assert a.size == b.size
        assert a.tobytes() != b.tobytes()


def test_readonly_directory_fails_cleanly(tmp_path):
    """A directory we cannot write to must yield a business error, no PNG."""
    readonly = tmp_path / "readonly"
    readonly.mkdir()
    scene_path = write_scene(tmp_path, scene_dict(2, 2, [0, 10, 10, 0], [5]))
    png = readonly / "preview.png"
    res = readonly / "result.json"

    import contour_tool.output as output

    def deny(*_a, **_k):
        raise OSError(13, "Permission denied")

    original = output.tempfile.mkstemp

    def fake_mkstemp(*args, **kwargs):
        directory = kwargs.get("dir") or (args[2] if len(args) > 2 else None)
        if directory == readonly:
            raise PermissionError(13, "Permission denied", str(directory))
        return original(*args, **kwargs)

    output.tempfile.mkstemp = fake_mkstemp
    try:
        from contour_tool.errors import OutputError

        with pytest.raises(OutputError, match="不可写"):
            run(str(scene_path), str(png), str(res))
    finally:
        output.tempfile.mkstemp = original

    assert not png.exists()
    assert not res.exists()
    assert list(readonly.iterdir()) == []


def test_failed_json_write_removes_the_new_png(tmp_path):
    """If the PNG lands but the JSON then fails, the new PNG is rolled back."""
    scene_path = write_scene(tmp_path, scene_dict(2, 2, [0, 10, 10, 0], [5]))
    out = tmp_path / "out"
    out.mkdir()
    png = out / "preview.png"
    res = out / "result.json"

    import contour_tool.output as output

    original = output._atomic_write

    def fail_on_json(path, writer):
        if Path(path) == res:
            raise output.OutputError("写入文件失败（磁盘已满），未留下不完整文件")
        return original(path, writer)

    output._atomic_write = fail_on_json
    try:
        from contour_tool.errors import OutputError

        with pytest.raises(OutputError, match="磁盘已满"):
            run(str(scene_path), str(png), str(res))
    finally:
        output._atomic_write = original

    assert not png.exists()
    assert not res.exists()
    assert list(out.iterdir()) == []


def test_failed_json_write_keeps_a_preexisting_png(tmp_path):
    scene_path = write_scene(tmp_path, scene_dict(2, 2, [0, 10, 10, 0], [5]))
    out = tmp_path / "out"
    out.mkdir()
    png = out / "preview.png"
    res = out / "result.json"
    png.write_bytes(b"previous run")

    import contour_tool.output as output

    original = output._atomic_write

    def fail_on_json(path, writer):
        if Path(path) == res:
            raise output.OutputError("写入文件失败（磁盘已满），未留下不完整文件")
        return original(path, writer)

    output._atomic_write = fail_on_json
    try:
        from contour_tool.errors import OutputError

        with pytest.raises(OutputError):
            run(str(scene_path), str(png), str(res))
    finally:
        output._atomic_write = original

    assert png.read_bytes() == b"previous run"


def test_failed_png_save_leaves_no_tmp_files(tmp_path):
    scene_path = write_scene(tmp_path, scene_dict(2, 2, [0, 10, 10, 0], [5]))
    out = tmp_path / "out"
    out.mkdir()
    png = out / "preview.png"
    res = out / "result.json"

    import contour_tool.output as output

    original = Image.Image.save

    def broken_save(self, fp, *args, **kwargs):
        if kwargs.get("format") == "PNG":
            raise OSError(28, "No space left on device")
        return original(self, fp, *args, **kwargs)

    Image.Image.save = broken_save
    try:
        from contour_tool.errors import OutputError

        with pytest.raises(OutputError, match="写入文件"):
            run(str(scene_path), str(png), str(res))
    finally:
        Image.Image.save = original
    assert list(out.iterdir()) == []
