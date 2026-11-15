"""需求 5：路径按清单目录解析，绝对路径与子目录相对路径均可定位。"""

from __future__ import annotations

from pathlib import Path

from animstrip.builder import build_strip
from animstrip.parser import load_manifest


def _spec(manifest_path, anim_id):
    parsed = {p.animation_id: p for p in load_manifest(manifest_path)}
    return parsed[anim_id].spec


def test_relative_subdir_path_resolved_against_manifest_dir(manifest_path, rows_by_id):
    row = rows_by_id["anim_alpha"]
    spec = _spec(manifest_path, "anim_alpha")
    image_field = spec.frames[0].image
    assert not Path(image_field).is_absolute()
    assert "/" in image_field  # 含子目录的相对路径
    assert spec.resolve_frame_path(spec.frames[0]).is_file()
    strip, _ = build_strip(spec)
    assert strip.width > 0
    assert row["strip"] == "anim_alpha.png"


def test_absolute_path_resolved(manifest_path, rows_by_id):
    # 需求 5 核对方式：两种路径写法各一个动画均成功生成条带
    row = rows_by_id["anim_loop"]
    spec = _spec(manifest_path, "anim_loop")
    assert all(Path(f.image).is_absolute() for f in spec.frames)
    assert all(spec.resolve_frame_path(f).is_file() for f in spec.frames)
    assert row["strip"] == "anim_loop.png"
    assert row["passed"] is True
