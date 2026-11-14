"""Requirements 2, 7, 10, 11: idempotence, previews, unverified entries, summary."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from PIL import Image

from subtitle_checker.cli import main
from subtitle_checker.preview import SAFE_BORDER, preview_filename
from tests.conftest import read_json


def tree_digest(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            result[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def test_inputs_unchanged_and_repeat_runs_identical(sample_root: Path, tmp_path: Path) -> None:
    before = tree_digest(sample_root)
    out_a = tmp_path / "a"
    out_b = tmp_path / "b"
    args = [str(sample_root / "subtitles.json"), str(sample_root / "frames")]
    assert main(args + [str(out_a)]) == 0
    assert main(args + [str(out_b)]) == 0
    assert tree_digest(sample_root) == before, "input sample tree must not change"
    assert tree_digest(out_a) == tree_digest(out_b)


def test_preview_canvas_and_safe_border(sample_root: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    assert main([str(sample_root / "subtitles.json"), str(sample_root / "frames"), str(out)]) == 0

    manifest = read_json(sample_root / "subtitles.json")
    width = manifest["canvas"]["width"]
    height = manifest["canvas"]["height"]
    margin = manifest["safe_margin"]
    left = margin["left"]
    top = margin["top"]
    right = width - margin["right"] - 1
    bottom = height - margin["bottom"] - 1

    for sub in manifest["subtitles"]:
        path = out / "previews" / preview_filename(sub["id"])
        if sub["id"] in {"S003", "S004"}:  # timing anomalies are never rendered
            assert not path.exists()
            continue
        assert path.exists(), path
        with Image.open(path) as img:
            assert img.size == (width, height)
            assert img.mode == "RGB"
            assert img.getpixel((left, top)) == SAFE_BORDER
            assert img.getpixel((right, top)) == SAFE_BORDER
            assert img.getpixel((left, bottom)) == SAFE_BORDER
            assert img.getpixel((right, bottom)) == SAFE_BORDER
            assert img.getpixel((left - 3, top + 5)) != SAFE_BORDER


def test_removing_one_frame_only_affects_that_entry(sample_root: Path, tmp_path: Path) -> None:
    frames = tmp_path / "frames"
    frames.mkdir()
    for path in (sample_root / "frames").glob("*.png"):
        (frames / path.name).write_bytes(path.read_bytes())

    out_full = tmp_path / "full"
    out_missing = tmp_path / "missing"
    manifest = str(sample_root / "subtitles.json")
    assert main([manifest, str(frames), str(out_full)]) == 0
    (frames / "S001.png").unlink()
    assert main([manifest, str(frames), str(out_missing)]) == 0

    full = read_json(out_full / "report.json")
    missing = read_json(out_missing / "report.json")
    groups = {g["key"]: g for g in missing["groups"]}
    assert "S001" in groups["unverified"]["ids"]
    assert any("帧图缺失" in reason for item in groups["unverified"]["items"] if item["id"] == "S001" for reason in item["reasons"])

    # Every other entry keeps its original group.
    def group_of(report: dict, sid: str) -> str:
        for group in report["groups"]:
            if sid in group["ids"]:
                return group["key"]
        raise AssertionError(sid)

    for group in full["groups"]:
        for sid in group["ids"]:
            if sid == "S001":
                continue
            assert group_of(missing, sid) == group["key"], sid


def test_summary_matches_detail(sample_root: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    assert main([str(sample_root / "subtitles.json"), str(sample_root / "frames"), str(out)]) == 0
    report = read_json(out / "report.json")
    summary = report["summary"]
    assert summary["total"] == sum(len(g["items"]) for g in report["groups"])
    assert summary["total"] == sum(summary[key] for key in ("pass", "overflow", "occlusion", "unverified", "timing"))
    by_key = {g["key"]: g for g in report["groups"]}
    for key in ("pass", "overflow", "occlusion", "unverified", "timing"):
        assert summary[key] == len(by_key[key]["items"])


def test_output_dir_created_and_previews_listed(sample_root: Path, tmp_path: Path) -> None:
    out = tmp_path / "nested" / "deeper" / "out"
    assert not out.exists()
    assert main([str(sample_root / "subtitles.json"), str(sample_root / "frames"), str(out)]) == 0
    assert (out / "report.json").is_file()
    assert (out / "report.md").is_file()
    previews = sorted((out / "previews").glob("subtitle_*.png"))
    assert len(previews) == 17  # 19 entries minus the 2 timing anomalies


def test_previews_removed_when_entry_becomes_timing_anomaly(tmp_path: Path) -> None:
    from tests.conftest import base_manifest, write_json

    frames = tmp_path / "frames"
    frames.mkdir()
    Image.new("RGB", (32, 32), (30, 40, 50)).save(frames / "A.png")
    Image.new("RGB", (32, 32), (30, 40, 50)).save(frames / "B.png")

    data = base_manifest()
    data["subtitles"][0]["id"] = "A"
    second = dict(data["subtitles"][0])
    second.update(id="B", start_ms=2000, end_ms=3000)
    data["subtitles"].append(second)
    manifest_path = write_json(tmp_path / "manifest.json", data)

    out = tmp_path / "out"
    assert main([str(manifest_path), str(frames), str(out)]) == 0
    assert (out / "previews" / "subtitle_B.png").exists()

    data["subtitles"][1]["end_ms"] = 2000  # B becomes zero-duration
    write_json(manifest_path, data)
    assert main([str(manifest_path), str(frames), str(out)]) == 0
    assert not (out / "previews" / "subtitle_B.png").exists(), "stale preview must be removed"
    assert (out / "previews" / "subtitle_A.png").exists()
    report = json.loads((out / "report.json").read_text(encoding="utf-8"))
    assert "B" in next(g for g in report["groups"] if g["key"] == "timing")["ids"]


def test_output_dir_inside_frames_is_rejected(sample_root: Path, tmp_path: Path, capsys) -> None:
    frames = tmp_path / "frames"
    frames.mkdir()
    Image.new("RGB", (32, 32)).save(frames / "S001.png")
    code = main([str(sample_root / "subtitles.json"), str(frames), str(frames / "out")])
    err = capsys.readouterr().err
    assert code == 2
    assert "output_dir" in err
    assert not (frames / "out").exists()
