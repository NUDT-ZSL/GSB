"""自动检查：把验收要点落实为可重复的断言。

运行：python -m pytest tests -q
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cover_selector.config import Config  # noqa: E402
from cover_selector.frames import locate_frame, scan_frames, target_frame_number  # noqa: E402
from cover_selector.pipeline import SelectionRun  # noqa: E402
from cover_selector.manifest import load_manifest  # noqa: E402
from cover_selector.report import write_report_files  # noqa: E402
from cover_selector.scoring import group_duplicates  # noqa: E402


def _config(**overrides) -> Config:
    base = dict(
        fps=25,
        base_width=320,
        base_height=180,
        dedupe_threshold=0.02,
        tolerance_frames=0.5,
    )
    base.update(overrides)
    return Config(**base)


def _save(path: Path, image: Image.Image) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)


def _busy(seed: int, size=(320, 180)) -> Image.Image:
    """随机棋盘格，几乎不存在重复。"""

    rng = np.random.default_rng(seed)
    cells = rng.integers(0, 2, size=(6, 10)).astype(np.uint8) * 255
    image = Image.fromarray(np.kron(cells, np.ones((size[1] // 6, size[0] // 10), np.uint8)))
    return image.convert("RGB")


def _flat(value: int = 128, size=(320, 180)) -> Image.Image:
    return Image.new("RGB", size, (value, value, value))


def _hash_dir(path: Path) -> dict[str, str]:
    result = {}
    for item in sorted(path.iterdir()):
        if item.is_file():
            result[item.name] = hashlib.sha256(item.read_bytes()).hexdigest()
    return result


# ---------------------------------------------------------------------------
def _make_frames(directory: Path, indices=range(10), size=(320, 180), missing=()):
    for index in indices:
        if index in missing:
            continue
        _save(directory / f"frame_{index:04d}.png", _busy(index, size))


def _run(tmp_path: Path, frames: Path, manifest: Path, config: Config, name="case"):
    entries = load_manifest(manifest)
    run = SelectionRun(
        frame_dir=frames,
        manifest_path=manifest,
        entries=entries,
        config=config,
        output_dir=tmp_path / "output",
        iteration=name,
    )
    run.prepare()
    report = run.execute()
    write_report_files(report, tmp_path / "output", None)
    payload = report.to_dict()
    return run, report, payload


def _write_manifest(path: Path, items) -> Path:
    path.write_text(json.dumps({"items": items}, ensure_ascii=False), encoding="utf-8")
    return path


def _write_config(path: Path, data: dict) -> Path:
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


# 1) 60 帧序列 + 5 个时间点，各映射到正确帧文件名 ---------------------------
def test_sixty_frames_map_to_correct_files(tmp_path):
    frames = tmp_path / "frames"
    for index in range(61):
        if index == 25:
            continue
        _save(frames / f"frame_{index:04d}.png", _busy(index))
    manifest = _write_manifest(
        tmp_path / "manifest.json",
        [
            {"label": "a", "time": 0.2},
            {"label": "b", "time": 1.2},
            {"label": "c", "time": 2.0},
            {"label": "d", "time": 2.4},
            {"label": "e", "time": 2.44},
        ],
    )
    _run(tmp_path, frames, manifest, _config())
    payload = json.loads((tmp_path / "output" / "covers_report.json").read_text("utf-8"))

    expected = {1: "frame_0005.png", 2: "frame_0030.png", 3: "frame_0050.png",
                4: "frame_0060.png", 5: "frame_0061.png"}
    # frame 61 不存在 → 第 5 条应在报告中列为缺失
    assert payload["entries"][0]["frame_file"] == expected[1]
    assert payload["entries"][1]["frame_file"] == expected[2]
    assert payload["entries"][2]["frame_file"] == expected[3]
    assert payload["entries"][3]["frame_file"] == expected[4]
    assert payload["entries"][4]["status"] == "error"
    assert payload["entries"][4]["frame_file"] is None
    assert "帧号 61" in (payload["entries"][4]["reasons"][0])


# 2) 帧率 25、时间 0.2s → 第 5 帧 -----------------------------------------
def test_fps_conversion_and_tolerance():
    config = _config(fps=25)
    assert target_frame_number(0.2, config) == 5
    assert target_frame_number(1.0, config) == 25
    index = {5: [Path("frame_0005.png")]}
    match, notes = locate_frame(0.2, config, index)
    assert match is not None and match.path.name == "frame_0005.png"

    # 容差内的相邻帧可以作为替代，但必须记录
    index = {4: [Path("frame_0004.png")]}
    match, notes = locate_frame(0.2, _config(tolerance_frames=1.0), index)
    assert match is not None and match.index == 4 and notes

    # 容差之外的帧不会被当作替代帧：目标 5、现有 3 与 7、容差 0.5
    match, notes = locate_frame(0.2, config, {3: [Path("f3.png")], 7: [Path("f7.png")]})
    assert match is None and "无对应帧文件" in notes[0]


# 3) 越界与缺失：报告原因，其余照常 ----------------------------------------
def test_out_of_range_and_missing_are_reported(tmp_path):
    frames = tmp_path / "frames"
    _make_frames(frames, range(10), missing=(5,))
    manifest = _write_manifest(
        tmp_path / "manifest.json",
        [{"label": "ok", "time": 0.12}, {"label": "missing", "time": 0.20},
         {"label": "far", "time": 9.0}],
    )
    _run(tmp_path, frames, manifest, _config())
    payload = json.loads((tmp_path / "output" / "covers_report.json").read_text("utf-8"))
    statuses = [entry["status"] for entry in payload["entries"]]
    assert statuses[0] == "selected"
    assert statuses[1] == "error" and statuses[2] == "error"
    assert "无对应帧文件" in payload["entries"][1]["reasons"][0]
    assert "超出帧序列范围" in payload["entries"][2]["reasons"][0]


# 4) 近似重复合并 ----------------------------------------------------------
def test_near_duplicates_are_merged(tmp_path):
    frames = tmp_path / "frames"
    _make_frames(frames, range(6))
    _save(frames / "frame_0005.png", _flat(140))
    _save(frames / "frame_0006.png", _flat(141))  # 与 5 几乎相同
    manifest = _write_manifest(
        tmp_path / "manifest.json",
        [{"label": "static_a", "time": 0.20}, {"label": "static_b", "time": 0.24},
         {"label": "busy", "time": 0.04}],
    )
    _run(tmp_path, frames, manifest, _config(dedupe_threshold=0.02))
    payload = json.loads((tmp_path / "output" / "covers_report.json").read_text("utf-8"))
    by_label = {entry["label"]: entry for entry in payload["entries"]}
    assert by_label["static_a"]["status"] == "selected"
    assert by_label["static_b"]["status"] == "merged"
    assert by_label["static_b"]["merged_into"] == by_label["static_a"]["output"]
    assert by_label["static_a"]["merged_into"] is None
    md = (tmp_path / "output" / "covers_report.md").read_text("utf-8")
    assert "已合并" in md and "0.24s" in md


# 5) 按画面差异强度排序 ----------------------------------------------------
def test_covers_sorted_by_difference_strength(tmp_path):
    frames = tmp_path / "frames"
    calm = Image.new("RGB", (320, 180), (90, 100, 110))
    busy = Image.new("RGB", (320, 180), (0, 0, 0))
    draw = ImageDraw.Draw(busy)
    for x in range(0, 320, 16):
        draw.rectangle([x, 0, x + 7, 179], fill=(255, 255, 255))
    _save(frames / "frame_0001.png", calm)
    _save(frames / "frame_0002.png", busy)
    manifest = _write_manifest(
        tmp_path / "manifest.json",
        [{"label": "calm", "time": 0.04}, {"label": "busy", "time": 0.08}],
    )
    _run(tmp_path, frames, manifest, _config())
    payload = json.loads((tmp_path / "output" / "covers_report.json").read_text("utf-8"))
    assert [cover["label"] for cover in payload["covers"]] == ["busy", "calm"]
    assert payload["covers"][0]["score"] > payload["covers"][1]["score"]
    assert [entry["rank"] for entry in payload["covers"]] == [1, 2]
    assert Path(payload["covers"][0]["output"]).name.startswith("cover_01_busy")
    md = (tmp_path / "output" / "covers_report.md").read_text("utf-8")
    assert "排序依据" in md and "评分" in md


# 6) 尺寸不一致不中断 -----------------------------------------------------
def test_inconsistent_frame_size_is_normalized(tmp_path):
    frames = tmp_path / "frames"
    _make_frames(frames, (1, 2, 3))
    _save(frames / "frame_0004.png", _busy(9, size=(160, 90)))
    manifest = _write_manifest(
        tmp_path / "manifest.json",
        [{"label": "odd", "time": 0.16}, {"label": "normal", "time": 0.08}],
    )
    _run(tmp_path, frames, manifest, _config())
    payload = json.loads((tmp_path / "output" / "covers_report.json").read_text("utf-8"))
    odd = payload["entries"][0]
    assert odd["status"] == "selected"
    assert "原尺寸 160x90" in odd["reasons"][0]
    assert payload["entries"][1]["status"] == "selected"
    out_image = Image.open(tmp_path / "output" / odd["output"])
    assert out_image.size == (320, 180)
    md = (tmp_path / "output" / "covers_report.md").read_text("utf-8")
    assert "尺寸统一记录" in md and "frame_0004.png" in md


# 7) 阈值改变合并结果；同阈值结果一致 -------------------------------------
def test_threshold_changes_merge_result_and_is_deterministic(tmp_path):
    frames = tmp_path / "frames"
    _make_frames(frames, range(4))
    _save(frames / "frame_0004.png", _flat(120))
    _save(frames / "frame_0005.png", _flat(150))
    manifest = _write_manifest(
        tmp_path / "manifest.json",
        [{"label": "a", "time": 0.16}, {"label": "b", "time": 0.20}],
    )
    loose_entries = load_manifest(manifest)
    loose = SelectionRun(frame_dir=frames, manifest_path=manifest, entries=loose_entries,
                         config=_config(dedupe_threshold=0.2), output_dir=tmp_path / "loose")
    loose.prepare()
    loose_report = loose.execute()
    assert loose_report.status_counts["merged"] == 1

    strict_entries = load_manifest(manifest)
    strict = SelectionRun(frame_dir=frames, manifest_path=manifest, entries=strict_entries,
                          config=_config(dedupe_threshold=0.001), output_dir=tmp_path / "strict")
    strict.prepare()
    strict_report = strict.execute()
    assert strict_report.status_counts["merged"] == 0
    assert strict_report.status_counts["selected"] == 2

    repeats = []
    for _ in range(2):
        entries = load_manifest(manifest)
        repeat = SelectionRun(frame_dir=frames, manifest_path=manifest, entries=entries,
                              config=_config(dedupe_threshold=0.2),
                              output_dir=tmp_path / "repeat")
        repeat.prepare()
        payload = repeat.execute().to_dict()
        repeats.append(
            [(c["output"], c["score"], [m["position"] for m in c["merged"]]) for c in payload["covers"]]
        )
    assert repeats[0] == repeats[1]


# 4b) 合并以代表帧为基准，不做链式传递 -----------------------------------
def test_duplicate_grouping_uses_representative_not_chaining():
    rng = np.random.default_rng(0)
    base = rng.integers(0, 255, size=(8, 8), dtype=np.uint8)
    a = base
    b = np.clip(base.astype(np.int16) + 20, 0, 255).astype(np.uint8)
    c = np.clip(base.astype(np.int16) + 40, 0, 255).astype(np.uint8)
    items = [
        {"position": 1, "time": 0.1, "gray": a, "score": 0.9},
        {"position": 2, "time": 0.2, "gray": b, "score": 0.8},
        {"position": 3, "time": 0.3, "gray": c, "score": 0.7},
    ]
    # a≈b（20/255≈0.078）与 b≈c（20/255）都在阈值内，但 a 与 c 相差 40/255≈0.157
    groups = group_duplicates(items, threshold=0.09)
    assert len(groups) == 2
    assert groups[0]["keeper"] == 0
    assert [m["position"] for m in groups[0]["merged"]] == [2]
    assert groups[1]["keeper"] == 2 and groups[1]["merged"] == []


# 8) 报告覆盖每个清单时间点 -----------------------------------------------
def test_report_maps_every_entry(tmp_path):
    frames = tmp_path / "frames"
    _make_frames(frames, range(6), missing=(3,))
    manifest = _write_manifest(
        tmp_path / "manifest.json",
        [{"label": "ok", "time": 0.08}, {"label": "missing", "time": 0.12},
         {"label": "reuse", "time": 0.092}, {"label": "far", "time": 100.0}],
    )
    _run(tmp_path, frames, manifest, _config(dedupe_threshold=0.005))
    payload = json.loads((tmp_path / "output" / "covers_report.json").read_text("utf-8"))
    assert len(payload["entries"]) == 4
    for entry in payload["entries"]:
        assert entry["status"] in {"selected", "merged", "skipped", "error"}
        if entry["status"] in {"selected", "merged"}:
            assert (tmp_path / "output" / entry["output"]).exists() or entry["merged_into"]
        else:
            assert entry["reasons"]
    assert payload["entries"][3]["reasons"]


# 9) 只写输出目录，不改源文件 ---------------------------------------------
def test_output_isolated_and_sources_unchanged(tmp_path):
    frames = tmp_path / "frames"
    _make_frames(frames, range(5))
    manifest = _write_manifest(tmp_path / "manifest.json", [{"label": "x", "time": 0.1}])
    manifest_hash = hashlib.sha256(manifest.read_bytes()).hexdigest()
    frame_hashes = _hash_dir(frames)

    _run(tmp_path, frames, manifest, _config())

    assert hashlib.sha256(manifest.read_bytes()).hexdigest() == manifest_hash
    assert _hash_dir(frames) == frame_hashes
    output_files = sorted(p.name for p in (tmp_path / "output").iterdir() if p.is_file())
    assert any(name.startswith("cover_01_x") for name in output_files)
    assert {"covers_report.json", "covers_report.md", "covers_report.html"} <= set(output_files)
    # 源目录未新增文件
    assert set(_hash_dir(frames)) == set(frame_hashes)


# 10) 非法条目跳过并记录原因 ----------------------------------------------
def test_invalid_entries_skipped_with_reason(tmp_path):
    frames = tmp_path / "frames"
    _make_frames(frames, range(4))
    manifest = _write_manifest(
        tmp_path / "manifest.json",
        [{"label": "ok", "time": 0.08}, {"label": "no_time", "note": "缺少时间"},
         {"label": "bad_scale", "time": "soon"}, {"label": "negative", "time": -0.5}],
    )
    _run(tmp_path, frames, manifest, _config())
    payload = json.loads((tmp_path / "output" / "covers_report.json").read_text("utf-8"))
    assert payload["entries"][0]["status"] == "selected"
    assert payload["entries"][1]["status"] == "skipped"
    assert "缺少 time" in payload["entries"][1]["reasons"][0]
    assert payload["entries"][2]["status"] == "skipped"
    assert "不是数值" in payload["entries"][2]["reasons"][0]
    assert payload["entries"][3]["status"] == "error"
    assert "超出帧序列范围" in payload["entries"][3]["reasons"][0]
    md = (tmp_path / "output" / "covers_report.md").read_text("utf-8")
    assert "no_time" in md and "缺少 time 字段" in md


def test_config_tolerance_seconds_and_validation(tmp_path):
    from cover_selector.config import load_config
    from cover_selector.errors import ConfigError

    base = {"fps": 25, "base_width": 320, "base_height": 180, "dedupe_threshold": 0.02}
    path = _write_config(tmp_path / "c.json", {**base, "tolerance_seconds": 0.02})
    assert load_config(path).tolerance_frames == pytest.approx(0.5)

    _write_config(
        tmp_path / "both.json", {**base, "tolerance_seconds": 0.02, "tolerance_frames": 0.5}
    )
    with pytest.raises(ConfigError):
        load_config(tmp_path / "both.json")

    _write_config(tmp_path / "unknown.json", {**base, "oops": 1})
    with pytest.raises(ConfigError):
        load_config(tmp_path / "unknown.json")

    _write_config(tmp_path / "bad.json", {**base, "fps": 0})
    with pytest.raises(ConfigError):
        load_config(tmp_path / "bad.json")


def test_cli_rejects_output_inside_frames(tmp_path):
    frames = tmp_path / "frames"
    _make_frames(frames, range(2))
    manifest = _write_manifest(tmp_path / "manifest.json", [{"label": "x", "time": 0.04}])
    config = _write_config(
        tmp_path / "config.json",
        {"fps": 25, "base_width": 320, "base_height": 180, "dedupe_threshold": 0.02},
    )
    result = subprocess.run(
        [sys.executable, "-m", "cover_selector", "--frames", str(frames),
         "--manifest", str(manifest), "--config", str(config),
         "--output", str(frames / "covers")],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
    )
    assert result.returncode == 2
    assert not (frames / "covers").exists()


# CLI 冒烟测试 ---------------------------------------------------------------
def test_cli_runs_end_to_end(tmp_path):
    frames = tmp_path / "frames"
    _make_frames(frames, range(4))
    manifest = _write_manifest(tmp_path / "manifest.json", [{"label": "x", "time": 0.12}])
    config = _write_config(
        tmp_path / "config.json",
        {"fps": 25, "base_width": 320, "base_height": 180, "dedupe_threshold": 0.02},
    )
    output = tmp_path / "out"
    result = subprocess.run(
        [sys.executable, "-m", "cover_selector", "--frames", str(frames),
         "--manifest", str(manifest), "--config", str(config), "--output", str(output)],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr
    assert (output / "covers_report.md").exists()
    assert (output / "covers_report.json").exists()
    assert (output / "covers_report.html").exists()
    assert list(output.glob("cover_01_x_*.png"))
