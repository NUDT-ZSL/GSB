"""Tests that the shipped business sample stays reproducible (requirement: sample < 10 MB)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from lip_sync import cli
from lip_sync.verify import AudioProbeCache, build_report, verify_all
from lip_sync.inputs import load_dialogues, load_frame_table

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "sample"


def _regenerate(tmp_path: Path) -> Path:
    out = tmp_path / "sample"
    subprocess.run(
        [sys.executable, str(ROOT / "tools" / "make_demo.py"), "--out", str(out)],
        check=True,
        capture_output=True,
    )
    return out


def test_sample_is_reproducible_and_small(tmp_path: Path) -> None:
    out = _regenerate(tmp_path)
    size = sum(p.stat().st_size for p in out.rglob("*") if p.is_file())
    assert size < 10 * 1024 * 1024
    frames = load_frame_table(out / "frames.json")
    dialogues = load_dialogues(out / "dialogues.json")
    assert len(dialogues.entries) == 8
    assert frames.earliest("L08") == 250.0


def test_sample_matches_expected_report(tmp_path: Path) -> None:
    out = _regenerate(tmp_path)
    expected = json.loads((out / "expected.json").read_text(encoding="utf-8"))["expected"]
    frames = load_frame_table(out / "frames.json")
    dialogues = load_dialogues(out / "dialogues.json")
    cache = AudioProbeCache()
    report = build_report(verify_all(frames, dialogues, 8.0, cache))
    lines = {line["line_id"]: line for line in report["lines"]}

    assert lines["L01"]["status"] == "out_of_tolerance"
    assert lines["L01"]["offset_ms"] == 10.0
    assert lines["L02"]["status"] == "ok"
    assert lines["L02"]["offset_ms"] == 5.0
    assert lines["L03"]["status"] == "silent"
    assert lines["L04"]["status"] == "unverifiable"
    assert lines["L04"]["reason"] == "channels_not_mono"
    assert lines["L05"]["status"] == "pending"
    assert lines["L05"]["reason"] == "missing_wav_path"
    assert lines["L06"]["offset_ms"] == -12.0
    assert lines["L07"]["offset_ms"] == 0.0
    assert lines["L08"]["mouth_time_ms"] == 250.0
    assert lines["L08"]["offset_ms"] == 10.0

    summary = report["summary"]
    assert summary["checked_lines"] == expected["summary"]["checked_lines"]
    assert summary["out_of_tolerance_lines"] == expected["summary"]["out_of_tolerance_lines"]
    assert summary["mean_offset_ms"] == 2.6
    assert summary["max_offset_ms"] == -12.0
    assert cache.decode_count == expected["decoded_wav_files"]

    ordered = [line["line_id"] for line in report["lines"]]
    assert ordered[:5] == ["L06", "L01", "L08", "L02", "L07"]


def test_checked_in_sample_runs(tmp_path: Path) -> None:
    sample_dir = SAMPLE if (SAMPLE / "frames.json").exists() else _regenerate(tmp_path)
    code = cli.main([
        "--frames", str(sample_dir / "frames.json"),
        "--dialogues", str(sample_dir / "dialogues.json"),
        "--tolerance-ms", "8",
        "--out-dir", str(tmp_path / "out"),
        "--quiet",
    ])
    assert code == 0
    report = json.loads((tmp_path / "out" / "lip_sync_report.json").read_text(encoding="utf-8"))
    assert len(report["lines"]) == 8
