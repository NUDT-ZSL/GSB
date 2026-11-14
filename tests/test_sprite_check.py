"""sprite_check 的本地测试，逐条覆盖任务核对方式 1-12。

所有图集与描述均由测试代码确定性生成，仅操作本地临时目录。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from PIL import Image

import sprite_check
from sprite_check import main, run


# ------------------------------------------------------------ 测试辅助

def write_atlas(path: Path, size, cells: dict[int, Image.Image],
                fw: int, fh: int, cols: int) -> Path:
    """生成图集：cells 为 {帧序号: 帧图像}，其余格全透明。"""
    atlas = Image.new("RGBA", size, (0, 0, 0, 0))
    for idx, cell in cells.items():
        ox, oy = (idx % cols) * fw, (idx // cols) * fh
        atlas.paste(cell, (ox, oy))
    atlas.save(path, format="PNG")
    return path


def solid(fw: int, fh: int, color) -> Image.Image:
    return Image.new("RGBA", (fw, fh), color)


def write_desc(path: Path, animations: list) -> Path:
    path.write_text(json.dumps({"animations": animations},
                               ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def anim(name, frames, fw=32, fh=32, threshold=0, loop=False):
    return {"name": name, "frame_width": fw, "frame_height": fh,
            "frames": frames, "diff_threshold": threshold, "loop": loop}


def get_anim(report: dict, name: str) -> dict:
    return next(a for a in report["animations"] if a["name"] == name)


RED = (200, 30, 30, 255)
BLUE = (30, 30, 200, 255)


# ------------------------------------------------------------ 1. 网格整除与帧序号校验

def test_grid_must_divide_atlas(tmp_path):
    atlas = tmp_path / "a.png"
    write_atlas(atlas, (64, 32), {0: solid(32, 32, RED)}, 32, 32, cols=2)
    desc = write_desc(tmp_path / "d.json", [anim("bad", [0, 1], fw=30, fh=32)])
    report = run(atlas, desc, tmp_path / "out")
    a = get_anim(report, "bad")
    assert a["status"] == "incomplete"
    assert any("does not divide" in e for e in a["errors"])
    assert a["preview"] is None


def test_duplicate_and_out_of_range_frames_reported(tmp_path):
    atlas = tmp_path / "a.png"
    write_atlas(atlas, (64, 32), {i: solid(32, 32, RED) for i in range(2)},
                32, 32, cols=2)
    desc = write_desc(tmp_path / "d.json", [anim("bad", [0, 1, 1, 7])])
    out = tmp_path / "out"
    report = run(atlas, desc, out)
    a = get_anim(report, "bad")
    assert a["status"] == "incomplete"
    assert any("duplicate" in e and "1" in e for e in a["errors"])
    assert any("out of range" in e and "7" in e for e in a["errors"])
    assert a["preview"] is None
    assert not (out / "bad_preview.png").exists()


# ------------------------------------------------------------ 2. 单像素脏矩形

def test_single_pixel_dirty_rect_exact(tmp_path):
    base = solid(32, 32, RED)
    changed = base.copy()
    changed.putpixel((5, 7), BLUE)
    atlas = tmp_path / "a.png"
    write_atlas(atlas, (64, 32), {0: base, 1: changed}, 32, 32, cols=2)
    desc = write_desc(tmp_path / "d.json", [anim("one", [0, 1])])
    report = run(atlas, desc, tmp_path / "out")
    a = get_anim(report, "one")
    assert a["status"] == "pass"
    entry = a["frames"][1]
    assert entry["changed_pixels"] == 1
    # 帧 1 位于图集 (32,0)，像素局部 (5,7) -> 图集 (37,7)，无冗余扩大
    assert entry["dirty_rect"] == {"lower_left": [37, 7],
                                   "upper_right": [37, 7]}


# ------------------------------------------------------------ 3. 低于阈值视为无变化

def test_below_threshold_keeps_previous_frame(tmp_path):
    f0 = solid(32, 32, RED)
    f1 = f0.copy()
    for x in range(3):  # 3 个像素差异，阈值 3 -> 未超过
        f1.putpixel((x, 0), BLUE)
    atlas = tmp_path / "a.png"
    write_atlas(atlas, (64, 32), {0: f0, 1: f1}, 32, 32, cols=2)
    desc = write_desc(tmp_path / "d.json", [anim("t", [0, 1], threshold=3)])
    out = tmp_path / "out"
    report = run(atlas, desc, out)
    a = get_anim(report, "t")
    assert a["frames"][1]["dirty_rect"] is None
    assert a["frames"][1]["changed_pixels"] == 3
    preview = Image.open(out / a["preview"]).convert("RGBA")
    slot0 = preview.crop((0, 0, 32, 32))
    slot1 = preview.crop((32, 0, 64, 32))
    assert slot1.tobytes() == slot0.tobytes()  # 保持前一帧内容


# ------------------------------------------------------------ 4. 循环跳变

def test_loop_jump_reported(tmp_path):
    f0 = solid(32, 32, RED)
    f1 = solid(32, 32, BLUE)
    f2 = solid(32, 32, (30, 200, 30, 255))
    atlas = tmp_path / "a.png"
    write_atlas(atlas, (96, 32), {0: f0, 1: f1, 2: f2}, 32, 32, cols=3)
    desc = write_desc(tmp_path / "d.json", [anim("loop", [0, 1, 2], loop=True)])
    report = run(atlas, desc, tmp_path / "out")
    a = get_anim(report, "loop")
    assert a["status"] == "jump"
    jump = a["loop_jump"]
    assert jump is not None and jump["type"] == "loop_jump"
    assert jump["pair"] == [2, 0]
    assert jump["changed_pixels"] == 32 * 32
    assert jump["dirty_rect"] == {"lower_left": [0, 31], "upper_right": [31, 0]}


def test_smooth_loop_passes(tmp_path):
    f0 = solid(32, 32, RED)
    atlas = tmp_path / "a.png"
    write_atlas(atlas, (64, 32), {0: f0, 1: f0.copy()}, 32, 32, cols=2)
    desc = write_desc(tmp_path / "d.json", [anim("ok", [0, 1], loop=True)])
    report = run(atlas, desc, tmp_path / "out")
    a = get_anim(report, "ok")
    assert a["status"] == "pass"
    assert a["loop_jump"] is None


# ------------------------------------------------------------ 5. 仅 alpha 差异计为变化

def test_alpha_only_change_detected(tmp_path):
    f0 = solid(32, 32, (0, 0, 0, 0))
    f0.putpixel((9, 11), (10, 20, 30, 0))     # 全透明
    f1 = solid(32, 32, (0, 0, 0, 0))
    f1.putpixel((9, 11), (10, 20, 30, 128))   # 仅 alpha 0 -> 128
    atlas = tmp_path / "a.png"
    write_atlas(atlas, (64, 32), {0: f0, 1: f1}, 32, 32, cols=2)
    desc = write_desc(tmp_path / "d.json", [anim("alpha", [0, 1])])
    report = run(atlas, desc, tmp_path / "out")
    entry = get_anim(report, "alpha")["frames"][1]
    assert entry["changed_pixels"] == 1
    rect = entry["dirty_rect"]
    assert rect["lower_left"][0] <= 32 + 9 <= rect["upper_right"][0]
    assert rect["upper_right"][1] <= 11 <= rect["lower_left"][1]


# ------------------------------------------------------------ 6. 拼接预览布局

def test_preview_layout_and_content(tmp_path):
    cells = {i: solid(16, 16, (i * 40, 0, 0, 255)) for i in range(3)}
    atlas = tmp_path / "a.png"
    write_atlas(atlas, (48, 16), cells, 16, 16, cols=3)
    desc = write_desc(tmp_path / "d.json",
                      [anim("p", [2, 0, 1], fw=16, fh=16, threshold=0)])
    out = tmp_path / "out"
    report = run(atlas, desc, out)
    a = get_anim(report, "p")
    preview = Image.open(out / a["preview"]).convert("RGBA")
    assert preview.size == (16 * 3, 16)  # 宽 = 帧宽 x 帧数
    atlas_img = Image.open(atlas).convert("RGBA")
    for seq, idx in enumerate([2, 0, 1]):
        ox = idx * 16
        assert (preview.crop((seq * 16, 0, seq * 16 + 16, 16)).tobytes()
                == atlas_img.crop((ox, 0, ox + 16, 16)).tobytes())


# ------------------------------------------------------------ 7. 图集缺失/损坏

def test_corrupt_atlas_still_reports(tmp_path):
    bad = tmp_path / "broken.png"
    bad.write_bytes(b"this is not a png file")
    desc = write_desc(tmp_path / "d.json",
                      [anim("x", [0, 1]), anim("y", [0], loop=True)])
    out = tmp_path / "out"
    report = run(bad, desc, out)  # 正常结束，不抛异常
    assert report["atlas"]["available"] is False
    assert report["atlas"]["error"]
    assert len(report["animations"]) == 2  # 描述仍被读取与核对
    for a in report["animations"]:
        assert a["status"] == "incomplete"
        assert any("atlas unavailable" in e for e in a["errors"])
    assert (out / "report.json").exists()


def test_missing_atlas_file(tmp_path):
    desc = write_desc(tmp_path / "d.json", [anim("x", [0])])
    report = run(tmp_path / "nonexistent.png", desc, tmp_path / "out")
    assert report["atlas"]["available"] is False
    assert "not found" in report["atlas"]["error"]


# ------------------------------------------------------------ 8. 单个动画不完整不影响其他

def test_incomplete_animation_does_not_block_others(tmp_path):
    cells = {0: solid(32, 32, RED), 1: solid(32, 32, BLUE)}
    atlas = tmp_path / "a.png"
    write_atlas(atlas, (64, 32), cells, 32, 32, cols=2)
    desc = write_desc(tmp_path / "d.json", [
        anim("broken", [0, 99]),   # 越界
        anim("good", [0, 1]),
    ])
    out = tmp_path / "out"
    report = run(atlas, desc, out)
    assert get_anim(report, "broken")["status"] == "incomplete"
    good = get_anim(report, "good")
    assert good["status"] == "pass"
    assert len(good["frames"]) == 2
    assert (out / good["preview"]).exists()


# ------------------------------------------------------------ 9. 报告字段与三种整体状态

def test_report_fields_and_three_statuses(tmp_path):
    f0 = solid(32, 32, RED)
    f1 = solid(32, 32, RED)   # 与帧 0 内容一致
    f2 = solid(32, 32, BLUE)
    atlas = tmp_path / "a.png"
    write_atlas(atlas, (96, 32), {0: f0, 1: f1, 2: f2}, 32, 32, cols=3)
    desc = write_desc(tmp_path / "d.json", [
        anim("ok", [0, 1], loop=True),        # 首尾一致 -> pass
        anim("seam", [0, 2], loop=True),      # 首尾不同 -> jump
        anim("bad", [0, 5]),                  # 越界 -> incomplete
    ])
    out = tmp_path / "out"
    report = run(atlas, desc, out)
    statuses = {a["name"]: a["status"] for a in report["animations"]}
    assert statuses == {"ok": "pass", "seam": "jump", "bad": "incomplete"}
    ok = get_anim(report, "ok")
    for key in ("name", "frame_count", "frames", "loop_jump", "status",
                "errors", "preview"):
        assert key in ok
    assert ok["frame_count"] == 2
    assert isinstance(ok["frames"], list) and len(ok["frames"]) == 2
    assert report["summary"] == {"total": 3, "pass": 1, "jump": 1,
                                 "incomplete": 1}
    # 磁盘上的 report.json 与返回值一致
    on_disk = json.loads((out / "report.json").read_text(encoding="utf-8"))
    assert on_disk == report


# ------------------------------------------------------------ 10. 图集坐标系换算

def test_dirty_rect_atlas_coordinates_with_offset(tmp_path):
    # 96x64 图集，32x32 帧 -> 3 列 2 行；帧 4 位于 (列1,行1)，原点 (32,32)
    f4 = solid(32, 32, RED)
    f5 = f4.copy()
    f5.putpixel((2, 1), BLUE)
    f5.putpixel((6, 4), BLUE)
    atlas = tmp_path / "a.png"
    write_atlas(atlas, (96, 64), {4: f4, 5: f5}, 32, 32, cols=3)
    desc = write_desc(tmp_path / "d.json", [anim("off", [4, 5])])
    report = run(atlas, desc, tmp_path / "out")
    rect = get_anim(report, "off")["frames"][1]["dirty_rect"]
    # 手工计算：帧 5 原点 (64,32)，局部 x∈[2,6] y∈[1,4]
    # -> 图集 x∈[66,70] y∈[33,36]；左下=(66,36)，右上=(70,33)
    assert rect == {"lower_left": [66, 36], "upper_right": [70, 33]}


# ------------------------------------------------------------ 11. 重复运行确定性

def test_repeated_runs_are_identical(tmp_path):
    cells = {i: solid(32, 32, (i * 60, 10, 200, 255)) for i in range(4)}
    atlas = tmp_path / "a.png"
    write_atlas(atlas, (128, 32), cells, 32, 32, cols=4)
    desc = write_desc(tmp_path / "d.json",
                      [anim("w", [0, 1, 2, 3], loop=True)])

    def digest(directory: Path) -> dict:
        return {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(directory.iterdir())}

    run(atlas, desc, tmp_path / "out1")
    run(atlas, desc, tmp_path / "out2")
    d1, d2 = digest(tmp_path / "out1"), digest(tmp_path / "out2")
    assert d1.keys() == d2.keys()
    assert d1 == d2  # 报告 JSON 与预览图哈希完全相同


# ------------------------------------------------------------ 12. 命令行接口

def test_cli_creates_output_dir_and_prints_summary(tmp_path, capsys):
    cells = {0: solid(32, 32, RED), 1: solid(32, 32, BLUE)}
    atlas = tmp_path / "atlas.png"
    write_atlas(atlas, (64, 32), cells, 32, 32, cols=2)
    desc = write_desc(tmp_path / "desc.json",
                      [anim("walk", [0, 1], loop=True)])
    out = tmp_path / "fresh" / "nested" / "out"  # 不存在，需自动创建
    rc = main(["--atlas", str(atlas), "--desc", str(desc), "--out", str(out)])
    assert rc == 0
    assert (out / "report.json").exists()
    assert (out / "walk_preview.png").exists()
    printed = capsys.readouterr().out
    assert "walk" in printed and "2 frame(s)" in printed
    assert "jump" in printed  # 红->蓝 循环跳变状态被打印


def test_cli_bad_desc_returns_error(tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    rc = main(["--atlas", str(tmp_path / "a.png"), "--desc", str(bad),
               "--out", str(tmp_path / "out")])
    assert rc == 1
    assert "error" in capsys.readouterr().err
