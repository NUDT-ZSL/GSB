"""端到端验收测试，逐条对应需求里的 10 条核对方式。

测试全部使用本地临时目录和确定性样例，不访问网络、不依赖容器。
运行：``python -m pytest -q``
"""

from __future__ import annotations

import hashlib
import json
import math
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from material_preview.runner import REPORT_NAME, run
from material_preview.samples import (
    SIZE,
    _base_color_rgb,
    _normal_rgb,
    _save_png,
    build,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
NEUTRAL = (128, 128, 255)
AMBIENT = 0.25


# --- 工具函数 -------------------------------------------------------------


def make_sample(tmp_path: Path, scenario: str) -> tuple[Path, Path]:
    sample_dir = tmp_path / f"sample_{scenario}"
    manifest = build(scenario, sample_dir)
    return manifest, sample_dir


def read_report(out_dir: Path) -> dict:
    return json.loads((out_dir / REPORT_NAME).read_text(encoding="utf-8"))


def png_files(out_dir: Path) -> list[Path]:
    return sorted(p for p in out_dir.glob("*.png"))


def decode(path: Path) -> np.ndarray:
    with Image.open(path) as image:
        image.load()
        assert image.format == "PNG"
        return np.asarray(image.convert("RGB"))


def sha256_dir(directory: Path) -> dict[str, str]:
    result = {}
    for path in sorted(directory.rglob("*")):
        if path.is_file():
            result[str(path.relative_to(directory))] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return result


def run_cli(manifest: Path, out_dir: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "material_preview", str(manifest), "-o", str(out_dir)],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )


# --- #1 逐项渲染，不遗漏、不合并 ------------------------------------------


def test_every_material_direction_gets_its_own_preview(tmp_path):
    manifest, _ = make_sample(tmp_path, "basic")
    out_dir = tmp_path / "out"

    report = run(manifest, out_dir)

    files = png_files(out_dir)
    assert len(files) == 6
    assert len({p.name for p in files}) == 6

    expected = {
        (m["name"], angle)
        for m in json.loads(manifest.read_text(encoding="utf-8"))["materials"]
        for angle in m["light_angles"]
    }
    actual = {
        (entry["material"], entry["angle"])
        for entry in report["entries"]
        if entry["status"] == "completed"
    }
    assert actual == expected
    assert report["summary"] == {
        "materials": 3,
        "entries": 6,
        "completed": 6,
        "incomplete": 0,
    }
    for entry in report["entries"]:
        preview = out_dir / entry["preview"]
        assert preview.is_file() and preview.stat().st_size > 0
        assert entry["material"] in preview.name and f"a{entry['angle']}" in preview.name
        assert decode(preview).shape == (SIZE, SIZE, 3)


# --- #2 越界/非数值方向不渲染，其他方向照常 --------------------------------


def test_out_of_range_angle_is_skipped_but_siblings_rendered(tmp_path):
    manifest, _ = make_sample(tmp_path, "bad_angle")
    out_dir = tmp_path / "out"

    report = run(manifest, out_dir)

    assert [p.name for p in png_files(out_dir)] == [
        "00_marble_a90.png",
        "01_brick_a45.png",
    ]

    by_key = {(e["material"], e["angle"]): e for e in report["entries"]}
    assert by_key[("marble", 90)]["status"] == "completed"
    assert by_key[("marble", 270)]["status"] == "incomplete"
    assert "超出" in by_key[("marble", 270)]["reason"]
    assert by_key[("brick", 45)]["status"] == "completed"

    marble = next(m for m in report["materials"] if m["name"] == "marble")
    assert marble["status"] == "partial"
    assert marble["completed_directions"] == 1 and marble["total_directions"] == 2


def test_non_numeric_and_nan_angles_are_rejected(tmp_path):
    manifest_path = tmp_path / "manifest.json"
    tex = tmp_path / "textures"
    _save_png(_base_color_rgb("marble"), tex / "marble_basecolor.png")
    _save_png(_normal_rgb(), tex / "marble_normal.png")
    manifest_path.write_text(
        json.dumps(
            {
                "root": ".",
                "light": {"angle_min": 0, "angle_max": 180},
                "materials": [
                    {
                        "name": "marble",
                        "base_color": "textures/marble_basecolor.png",
                        "normal_map": "textures/marble_normal.png",
                        "light_angles": ["north", True, 90],
                        "width": SIZE,
                        "height": SIZE,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    out_dir = tmp_path / "out"
    report = run(manifest_path, out_dir)

    statuses = {e["angle"]: e["status"] for e in report["entries"]}
    assert statuses == {"north": "incomplete", True: "incomplete", 90: "completed"}
    assert [p.name for p in png_files(out_dir)] == ["00_marble_a90.png"]


# --- #3 贴图缺失：该材质全部方向未完成，其他材质继续 ------------------------


def test_missing_normal_map_fails_only_that_material(tmp_path):
    manifest, _ = make_sample(tmp_path, "missing")
    out_dir = tmp_path / "out"

    report = run(manifest, out_dir)

    assert len(png_files(out_dir)) == 3  # marble 2 张 + brick 1 张
    glass_entries = [e for e in report["entries"] if e["material"] == "glass"]
    assert len(glass_entries) == 2
    assert all(e["status"] == "incomplete" for e in glass_entries)
    assert all("法线贴图缺失" in e["reason"] for e in glass_entries)
    assert all(e["preview"] is None for e in glass_entries)

    glass = next(m for m in report["materials"] if m["name"] == "glass")
    assert glass["status"] == "incomplete"
    assert glass["completed_directions"] == 0
    assert "法线贴图缺失" in glass["reason"]

    completed = [e for e in report["entries"] if e["status"] == "completed"]
    assert {e["material"] for e in completed} == {"marble", "brick"}


def test_missing_base_color_map_also_fails_material(tmp_path):
    manifest, sample_dir = make_sample(tmp_path, "missing")
    (sample_dir / "textures" / "brick_basecolor.png").unlink()
    out_dir = tmp_path / "out"

    report = run(manifest, out_dir)

    brick_entries = [e for e in report["entries"] if e["material"] == "brick"]
    assert all(e["status"] == "incomplete" for e in brick_entries)
    assert all("基础色贴图缺失" in e["reason"] for e in brick_entries)
    assert all(e["material"] != "brick" for e in report["entries"] if e["status"] == "completed")


# --- #4 尺寸不符：该材质不产出任何预览 ------------------------------------


def test_size_mismatch_produces_no_preview_for_material(tmp_path):
    manifest, _ = make_sample(tmp_path, "size")
    out_dir = tmp_path / "out"

    report = run(manifest, out_dir)

    assert png_files(out_dir) == []
    fabric = report["materials"][0]
    assert fabric["name"] == "fabric"
    assert fabric["status"] == "incomplete"
    assert "尺寸" in fabric["reason"]
    assert all(e["status"] == "incomplete" and e["preview"] is None for e in report["entries"])


def test_size_mismatch_between_two_textures_is_reported(tmp_path):
    manifest_path = tmp_path / "manifest.json"
    tex = tmp_path / "textures"
    _save_png(_base_color_rgb("fabric"), tex / "base.png")
    _save_png(_normal_rgb(size=SIZE // 2), tex / "normal.png")
    manifest_path.write_text(
        json.dumps(
            {
                "root": ".",
                "light": {"angle_min": 0, "angle_max": 180},
                "materials": [
                    {
                        "name": "fabric",
                        "base_color": "textures/base.png",
                        "normal_map": "textures/normal.png",
                        "light_angles": [0, 90],
                        "width": SIZE,
                        "height": SIZE,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    out_dir = tmp_path / "out"

    report = run(manifest_path, out_dir)

    assert png_files(out_dir) == []
    assert "16x16" in report["materials"][0]["reason"]
    assert len(report["entries"]) == 2
    assert all(e["status"] == "incomplete" for e in report["entries"])


# --- #5 零向量法线输出约定中性色，整图仍可生成 ----------------------------


def test_zero_normal_pixels_use_neutral_color(tmp_path):
    manifest, _ = make_sample(tmp_path, "zero_normal")
    out_dir = tmp_path / "out"

    report = run(manifest, out_dir)

    files = png_files(out_dir)
    assert len(files) == 2
    assert report["summary"]["completed"] == 2

    lo, hi = SIZE // 2 - 3, SIZE // 2 + 3
    for path in files:
        pixels = decode(path)
        patch = pixels[lo:hi, lo:hi].reshape(-1, 3)
        assert np.all(patch == np.array(NEUTRAL, dtype=np.uint8)), path.name
        assert not np.isnan(pixels.astype(float)).any()
        # 零向量以外的像素仍然带明暗差异，说明整图正常渲染。
        assert len(np.unique(pixels.reshape(-1, 3), axis=0)) > 10


# --- #6 各方向共用同一份基础色/法线数据 -----------------------------------


def _flat_normal(size: int = SIZE) -> np.ndarray:
    pixels = np.full((size, size, 3), 128, dtype=np.uint8)
    pixels[..., 2] = 255
    return pixels


def test_directions_share_same_texture_data(tmp_path):
    """全平面法线时，水平光几乎照不亮像素：

    * 0° 与 90° 的照射结果完全相同（法线 x、y 分量相等），
    * 180° 是背面，只剩环境光，必须逐像素等于基础色 × 环境光，
      证明纹理没有被平移、翻转或重采样。
    """
    manifest_path = tmp_path / "manifest.json"
    tex = tmp_path / "textures"
    base = _base_color_rgb("metal")
    base[5:9, 5:9] = [250, 10, 10]  # 定位用的红色标记
    _save_png(base, tex / "base.png")
    _save_png(_flat_normal(), tex / "normal.png")
    manifest_path.write_text(
        json.dumps(
            {
                "root": ".",
                "light": {"angle_min": 0, "angle_max": 180},
                "materials": [
                    {
                        "name": "metal",
                        "base_color": "textures/base.png",
                        "normal_map": "textures/normal.png",
                        "light_angles": [0, 90, 180],
                        "width": SIZE,
                        "height": SIZE,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    out_dir = tmp_path / "out"
    report = run(manifest_path, out_dir)

    by_angle = {e["angle"]: decode(out_dir / e["preview"]) for e in report["entries"]}
    assert set(by_angle) == {0, 90, 180}

    assert np.array_equal(by_angle[0], by_angle[90])

    # 180° 下每个像素都等于同一位置的基础色 × 环境光。
    expected = np.rint(base.astype(np.float64) * AMBIENT).clip(0, 255).astype(np.uint8)
    assert np.abs(by_angle[180].astype(int) - expected.astype(int)).max() <= 1
    # 红标记仍在原来的位置。
    mark = by_angle[180][5:9, 5:9]
    assert mark[..., 0].min() > 40 and mark[..., 1].max() < 20
    assert by_angle[0][5:9, 5:9, 0].min() > mark[..., 0].min()


def test_directions_differ_only_by_lighting(tmp_path):
    manifest, sample_dir = make_sample(tmp_path, "basic")
    out_dir = tmp_path / "out"
    report = run(manifest, out_dir)

    entries = {(e["material"], e["angle"]): e for e in report["entries"]}
    base = decode(sample_dir / "textures" / "marble_basecolor.png").astype(np.float64)
    normal_pixels = decode(sample_dir / "textures" / "marble_normal.png")
    vectors = normal_pixels.astype(np.float64) / 127.5 - 1.0
    vectors /= np.linalg.norm(vectors, axis=2, keepdims=True)

    for angle in (0, 90):
        pixels = decode(out_dir / entries[("marble", angle)]["preview"]).astype(np.float64)
        light = np.array([math.cos(math.radians(angle)), math.sin(math.radians(angle)), 0.0])
        intensity = AMBIENT + (1 - AMBIENT) * np.clip(
            (vectors * light).sum(axis=2), 0.0, 1.0
        )
        expected = np.rint(base * intensity[..., None]).clip(0, 255)
        # 渲染值与该方向的理论值逐像素一致，位置没有任何偏移。
        assert np.abs(pixels - expected).max() <= 1

    a0 = decode(out_dir / entries[("marble", 0)]["preview"])
    a90 = decode(out_dir / entries[("marble", 90)]["preview"])
    assert not np.array_equal(a0, a90), "不同光照方向应当产生不同的明暗"


# --- #7 后续失败不影响先前预览 ---------------------------------------------


def test_late_failure_does_not_damage_earlier_previews(tmp_path):
    manifest, _ = make_sample(tmp_path, "missing")  # glass 是最后一个材质
    out_dir = tmp_path / "out"
    report = run(manifest, out_dir)

    completed = [e for e in report["entries"] if e["status"] == "completed"]
    assert completed, "前面的材质应当已成功渲染"
    for entry in completed:
        path = out_dir / entry["preview"]
        assert path.stat().st_size > 0
        with Image.open(path) as image:
            image.load()
            assert image.format == "PNG"

    # 目录里不存在空文件或临时文件。
    for path in out_dir.iterdir():
        assert path.stat().st_size > 0, path
        assert not path.name.startswith(".preview-tmp-"), path


# --- #8 报告与磁盘实际结果一致 ---------------------------------------------


def test_report_matches_disk_state(tmp_path):
    manifest, _ = make_sample(tmp_path, "all")
    raw = json.loads(manifest.read_text(encoding="utf-8"))
    out_dir = tmp_path / "out"

    report = run(manifest, out_dir)

    total_from_manifest = sum(len(m["light_angles"]) for m in raw["materials"])
    assert len(report["entries"]) == total_from_manifest
    assert report["summary"]["entries"] == total_from_manifest

    for entry in report["entries"]:
        path = out_dir / entry["preview"] if entry["preview"] else None
        if entry["status"] == "completed":
            assert path is not None and path.is_file() and path.stat().st_size > 0
        else:
            assert entry["preview"] is None
            assert entry["reason"]

    # 报告里标注完成的条目，集合与磁盘上的 PNG 一一对应。
    reported = {e["preview"] for e in report["entries"] if e["status"] == "completed"}
    on_disk = {p.name for p in png_files(out_dir)}
    assert reported == on_disk


# --- #9 输入不变、重复运行结果一致 -----------------------------------------


def test_repeat_runs_are_byte_identical_and_inputs_untouched(tmp_path):
    manifest, sample_dir = make_sample(tmp_path, "all")
    before = sha256_dir(sample_dir)
    out_a = tmp_path / "out_a"
    out_b = tmp_path / "out_b"

    run(manifest, out_a)
    run(manifest, out_b)

    assert sha256_dir(sample_dir) == before, "运行不得修改输入的清单与贴图"

    a_files = sha256_dir(out_a)
    b_files = sha256_dir(out_b)
    assert a_files == b_files
    assert a_files, "应当有输出可比较"

    # 同一目录连续跑两次也要一致。
    first = sha256_dir(out_a)
    run(manifest, out_a)
    assert sha256_dir(out_a) == first


def test_rerun_does_not_keep_stale_previews(tmp_path):
    manifest, sample_dir = make_sample(tmp_path, "missing")
    out_dir = tmp_path / "out"
    run(manifest, out_dir)
    assert len(png_files(out_dir)) == 3

    # 修好缺失的法线贴图后重跑，glass 的预览应出现。
    _save_png(_normal_rgb(), sample_dir / "textures" / "glass_normal.png")
    report = run(manifest, out_dir)

    glass = [e for e in report["entries"] if e["material"] == "glass"]
    assert all(e["status"] == "completed" for e in glass)
    assert len(png_files(out_dir)) == 5


def test_unrelated_files_in_output_dir_are_preserved(tmp_path):
    manifest, _ = make_sample(tmp_path, "basic")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    keep = out_dir / "notes.txt"
    keep.write_text("do not delete", encoding="utf-8")

    run(manifest, out_dir)

    assert keep.read_text(encoding="utf-8") == "do not delete"
    assert len(png_files(out_dir)) == 6


# --- #10 清单语法错误：可读报错、非零退出、无残留预览 ----------------------


def test_syntax_error_cli_reports_and_leaves_no_previews(tmp_path):
    manifest, sample_dir = make_sample(tmp_path, "syntax_error")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    # 模拟上一次运行留下的产物
    (out_dir / "00_marble_a0.png").write_bytes(b"stale")
    (out_dir / REPORT_NAME).write_text("{}", encoding="utf-8")

    result = run_cli(manifest, out_dir)

    assert result.returncode != 0
    assert "语法错误" in result.stderr
    assert "行" in result.stderr
    assert png_files(out_dir) == []
    assert not (out_dir / REPORT_NAME).exists()
    # 输入文件没有被销毁
    assert (sample_dir / "manifest.json").exists()
    assert (sample_dir / "textures" / "marble_basecolor.png").exists()


def test_structurally_invalid_manifest_is_rejected_before_rendering(tmp_path):
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps({"materials": [{"name": "marble"}]}), encoding="utf-8"
    )
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (out_dir / "00_marble_a0.png").write_bytes(b"stale")
    result = run_cli(manifest_path, out_dir)

    assert result.returncode != 0
    assert "缺少必填字段" in result.stderr
    assert png_files(out_dir) == []


def test_unsafe_output_dir_is_refused(tmp_path):
    manifest, sample_dir = make_sample(tmp_path, "basic")
    before = sha256_dir(sample_dir)

    result = run_cli(manifest, sample_dir / "textures")

    assert result.returncode != 0
    assert "输出目录" in result.stderr
    assert sha256_dir(sample_dir) == before


def test_duplicate_material_names_are_rejected(tmp_path):
    manifest, sample_dir = make_sample(tmp_path, "basic")
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["materials"][1]["name"] = data["materials"][0]["name"]
    manifest.write_text(json.dumps(data), encoding="utf-8")
    out_dir = tmp_path / "out"

    result = run_cli(manifest, out_dir)

    assert result.returncode != 0
    assert "重复" in result.stderr
    assert png_files(out_dir) == []


def test_duplicate_angles_get_distinct_previews(tmp_path):
    """清单里列了两次同一角度时，两条条目都应有产物，不能互相覆盖。"""
    manifest, sample_dir = make_sample(tmp_path, "basic")
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["materials"][0]["light_angles"] = [0, 90, 90]
    manifest.write_text(json.dumps(data), encoding="utf-8")
    out_dir = tmp_path / "out"

    report = run(manifest, out_dir)

    marble = [e for e in report["entries"] if e["material"] == "marble"]
    assert len(marble) == 3
    assert all(e["status"] == "completed" for e in marble)
    assert len({e["preview"] for e in marble}) == 3
    for entry in marble:
        assert (out_dir / entry["preview"]).stat().st_size > 0


def test_material_name_case_collision_is_rejected(tmp_path):
    manifest, sample_dir = make_sample(tmp_path, "basic")
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["materials"][1]["name"] = data["materials"][0]["name"].upper()
    manifest.write_text(json.dumps(data), encoding="utf-8")

    result = run_cli(manifest, tmp_path / "out")

    assert result.returncode != 0
    assert "重复" in result.stderr


# --- 样例自身的确定性 ------------------------------------------------------


def test_overflowing_angle_stays_json_safe(tmp_path):
    """1e400 会被解析成 inf：不得渲染，报告仍必须是严格合法的 JSON。"""
    manifest_path = tmp_path / "manifest.json"
    tex = tmp_path / "textures"
    _save_png(_base_color_rgb("marble"), tex / "base.png")
    _save_png(_normal_rgb(), tex / "normal.png")
    manifest_path.write_text(
        (
            '{\n "root": ".",\n "light": {"angle_min": 0, "angle_max": 180},\n'
            ' "materials": [{"name": "marble", "base_color": "textures/base.png",\n'
            '  "normal_map": "textures/normal.png", "light_angles": [1e400, 90],\n'
            f'  "width": {SIZE}, "height": {SIZE}}}]\n}}\n'
        ),
        encoding="utf-8",
    )
    out_dir = tmp_path / "out"

    report = run(manifest_path, out_dir)

    text = (out_dir / REPORT_NAME).read_text(encoding="utf-8")
    assert "Infinity" not in text and "NaN" not in text
    json.loads(text, parse_constant=pytest.fail)

    statuses = {str(e["angle"]): e["status"] for e in report["entries"]}
    assert statuses["90"] == "completed"
    assert statuses["inf"] == "incomplete"
    assert [p.name for p in png_files(out_dir)] == ["00_marble_a90.png"]


def test_nan_literal_is_a_syntax_error(tmp_path):
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        '{"light": {"angle_min": 0, "angle_max": 180}, '
        '"materials": [{"name": "x", "base_color": "a.png", "normal_map": "b.png", '
        '"light_angles": [NaN], "width": 4, "height": 4}]}',
        encoding="utf-8",
    )

    result = run_cli(manifest_path, tmp_path / "out")

    assert result.returncode != 0
    assert "语法错误" in result.stderr
    assert png_files(tmp_path / "out") == []


def test_sample_generation_is_deterministic(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    build("all", first)
    build("all", second)

    assert sha256_dir(first) == sha256_dir(second)
    # 贴图本身也应是合法的 PNG。
    for png in first.glob("textures/*.png"):
        assert decode(png).shape[2] == 3

