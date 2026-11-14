"""Requirement 1 and 12: manifest validation with readable, field-named errors."""

from __future__ import annotations

from pathlib import Path

import pytest

from subtitle_checker.cli import main
from subtitle_checker.manifest import ManifestError, load_manifest, validate_manifest
from tests.conftest import base_manifest, write_json


@pytest.mark.parametrize(
    "mutate, field",
    [
        (lambda m: m["canvas"].update(width=-1080), "canvas.width"),
        (lambda m: m["canvas"].update(height=0), "canvas.height"),
        (lambda m: m["safe_margin"].update(top=961), "safe_margin.top"),
        (lambda m: m["safe_margin"].update(bottom=961), "safe_margin.bottom"),
        (lambda m: m["safe_margin"].update(left=541), "safe_margin.left"),
        (lambda m: m["safe_margin"].update(right=541), "safe_margin.right"),
        (lambda m: m["safe_margin"].update(top=0), "safe_margin.top"),
        (lambda m: m["safe_margin"].update(left=-1), "safe_margin.left"),
        (lambda m: m["canvas"].pop("width"), "canvas.width"),
        (lambda m: m.pop("safe_margin"), "safe_margin"),
        (lambda m: m.pop("fx_region"), "fx_region"),
        (lambda m: m["fx_region"].update(width=0), "fx_region.width"),
        (lambda m: m["subtitles"][0].pop("start_ms"), "subtitles[0].start_ms"),
        (lambda m: m["subtitles"][0]["anchor"].pop("y"), "subtitles[0].anchor.y"),
        (lambda m: m["subtitles"][0].update(font_size=0), "subtitles[0].font_size"),
    ],
)
def test_invalid_manifest_names_field(tmp_path: Path, mutate, field: str) -> None:
    data = base_manifest()
    mutate(data)
    path = write_json(tmp_path / "bad.json", data)
    with pytest.raises(ManifestError) as excinfo:
        load_manifest(path)
    assert any(field in message for message in excinfo.value.messages), excinfo.value.messages


def test_margin_exactly_half_canvas_is_accepted(tmp_path: Path) -> None:
    data = base_manifest()
    data["safe_margin"] = {"top": 960, "right": 540, "bottom": 960, "left": 540}
    manifest = validate_manifest(data, tmp_path / "ok.json")
    assert manifest.margin.top == 960


def test_margin_one_over_half_is_rejected(tmp_path: Path) -> None:
    data = base_manifest()
    data["safe_margin"]["top"] = 961
    with pytest.raises(ManifestError) as excinfo:
        validate_manifest(data, tmp_path / "bad.json")
    message = "\n".join(excinfo.value.messages)
    assert "safe_margin.top" in message
    assert "一半" in message


def test_duplicate_ids_are_rejected(tmp_path: Path) -> None:
    data = base_manifest()
    data["subtitles"].append(dict(data["subtitles"][0]))
    with pytest.raises(ManifestError) as excinfo:
        validate_manifest(data, tmp_path / "dup.json")
    assert any("subtitles[1].id" in m and "重复" in m for m in excinfo.value.messages)


def test_non_json_file_reports_parse_error(tmp_path: Path) -> None:
    path = tmp_path / "broken.json"
    path.write_text("{ not json", encoding="utf-8")
    with pytest.raises(ManifestError) as excinfo:
        load_manifest(path)
    assert "JSON 解析失败" in excinfo.value.messages[0]


def test_cli_rejects_invalid_manifest_without_writing(tmp_path: Path, capsys) -> None:
    data = base_manifest()
    data["canvas"]["width"] = -1
    manifest = write_json(tmp_path / "bad.json", data)
    frames = tmp_path / "frames"
    frames.mkdir()
    out = tmp_path / "out"
    code = main([str(manifest), str(frames), str(out)])
    captured = capsys.readouterr()
    assert code == 2
    assert "canvas.width" in captured.err
    assert not out.exists()


def test_cli_missing_manifest_does_not_crash(tmp_path: Path, capsys) -> None:
    frames = tmp_path / "frames"
    frames.mkdir()
    code = main([str(tmp_path / "nope.json"), str(frames), str(tmp_path / "out")])
    captured = capsys.readouterr()
    assert code == 2
    assert "文件不存在" in captured.err
