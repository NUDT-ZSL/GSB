"""End-to-end tests through the CLI (requirements 1, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13)."""

from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
import pytest

from lip_sync import cli
from lip_sync.verify import AudioProbeCache, build_report, verify_all
from lip_sync.inputs import load_dialogues, load_frame_table

from .helpers import SAMPLE_RATE, make_dialogues, make_frames, tone, write_json, write_wav


def _dataset(tmp_path: Path, offsets: dict[str, float], tolerance_ms: float = 8.0,
             onset_ms: float = 200.0) -> tuple[Path, Path]:
    """Build frames/dialogues whose mouth times are onset + offset."""
    wav = write_wav(tmp_path / "wav" / "line.wav", tone(onset_ms=onset_ms))
    frames = {"frames": [{"line_id": lid, "mouth_time_ms": onset_ms + off} for lid, off in offsets.items()]}
    dialogues = {
        "dialogues": [
            {"line_id": lid, "wav_path": str(wav.relative_to(tmp_path))} for lid in offsets
        ]
    }
    return write_json(tmp_path / "frames.json", frames), write_json(tmp_path / "dialogues.json", dialogues)


def _run(tmp_path: Path, frames: Path, dialogues: Path, tolerance: float = 8.0) -> tuple[int, dict, str]:
    out_dir = tmp_path / "out"
    import io
    from contextlib import redirect_stdout, redirect_stderr

    buf_out, buf_err = io.StringIO(), io.StringIO()
    with redirect_stdout(buf_out), redirect_stderr(buf_err):
        code = cli.main([
            "--frames", str(frames),
            "--dialogues", str(dialogues),
            "--tolerance-ms", str(tolerance),
            "--out-dir", str(out_dir),
        ])
    report_path = out_dir / "lip_sync_report.json"
    report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else {}
    return code, report, buf_out.getvalue() + buf_err.getvalue()


def _by_id(report: dict) -> dict[str, dict]:
    return {line["line_id"]: line for line in report["lines"]}


# (1) missing dialogue path -> that line is a pending problem, others still reported.
def test_missing_dialogue_path_isolated(tmp_path: Path) -> None:
    wav = write_wav(tmp_path / "wav" / "a.wav", tone(onset_ms=200.0))
    frames = write_json(tmp_path / "frames.json", make_frames({
        "A": [210.0], "B": [205.0], "C": [210.0],
    }))
    dialogues = write_json(tmp_path / "dialogues.json", make_dialogues([
        {"line_id": "A", "wav_path": "wav/a.wav"},
        {"line_id": "B", "wav_path": "wav/a.wav"},
        {"line_id": "C"},  # path removed
    ]))
    code, report, text = _run(tmp_path, frames, dialogues)
    assert code == 0
    lines = _by_id(report)
    assert lines["A"]["status"] == "out_of_tolerance"
    assert lines["B"]["status"] == "ok"
    assert lines["C"]["status"] == "pending"
    assert lines["C"]["reason"] == "missing_wav_path"


# (3) signed offset, +10 ms, sign correct.
def test_signed_offset_positive(tmp_path: Path) -> None:
    wav = write_wav(tmp_path / "wav" / "a.wav", tone(onset_ms=200.0))
    frames = write_json(tmp_path / "frames.json", make_frames({"A": [210.0]}))
    dialogues = write_json(tmp_path / "dialogues.json", make_dialogues([
        {"line_id": "A", "wav_path": "wav/a.wav"},
    ]))
    _, report, _ = _run(tmp_path, frames, dialogues)
    line = _by_id(report)["A"]
    assert line["offset_ms"] == pytest.approx(10.0, abs=1.0)
    assert line["offset_ms"] > 0


def test_signed_offset_negative(tmp_path: Path) -> None:
    wav = write_wav(tmp_path / "wav" / "a.wav", tone(onset_ms=200.0))
    frames = write_json(tmp_path / "frames.json", make_frames({"A": [188.0]}))
    dialogues = write_json(tmp_path / "dialogues.json", make_dialogues([
        {"line_id": "A", "wav_path": "wav/a.wav"},
    ]))
    _, report, _ = _run(tmp_path, frames, dialogues)
    line = _by_id(report)["A"]
    assert line["offset_ms"] == pytest.approx(-12.0, abs=1.0)
    assert line["offset_ms"] < 0


# (4) tolerance threshold from the CLI.
def test_tolerance_boundary(tmp_path: Path) -> None:
    write_wav(tmp_path / "wav" / "a.wav", tone(onset_ms=200.0))
    frames = write_json(tmp_path / "frames.json", make_frames({"LATE": [212.0], "NEAR": [205.0]}))
    dialogues = write_json(tmp_path / "dialogues.json", make_dialogues([
        {"line_id": "LATE", "wav_path": "wav/a.wav"},
        {"line_id": "NEAR", "wav_path": "wav/a.wav"},
    ]))
    _, report, _ = _run(tmp_path, frames, dialogues, tolerance=8.0)
    lines = _by_id(report)
    assert lines["LATE"]["status"] == "out_of_tolerance"
    assert lines["NEAR"]["status"] == "ok"


def test_tolerance_exactly_equal_is_ok(tmp_path: Path) -> None:
    # "exceeds tolerance" -> equality is not out of tolerance.
    write_wav(tmp_path / "wav" / "a.wav", tone(onset_ms=200.0))
    frames = write_json(tmp_path / "frames.json", make_frames({"A": [208.0]}))
    dialogues = write_json(tmp_path / "dialogues.json", make_dialogues([
        {"line_id": "A", "wav_path": "wav/a.wav"},
    ]))
    _, report, _ = _run(tmp_path, frames, dialogues, tolerance=8.0)
    assert _by_id(report)["A"]["status"] == "ok"


# (5) silent line, excluded from statistics.
def test_silent_line_listed_and_excluded(tmp_path: Path) -> None:
    write_wav(tmp_path / "wav" / "tone.wav", tone(onset_ms=200.0))
    write_wav(tmp_path / "wav" / "zero.wav", np.zeros(SAMPLE_RATE, dtype=np.float64))
    frames = write_json(tmp_path / "frames.json", make_frames({"A": [205.0], "Z": [300.0]}))
    dialogues = write_json(tmp_path / "dialogues.json", make_dialogues([
        {"line_id": "A", "wav_path": "wav/tone.wav"},
        {"line_id": "Z", "wav_path": "wav/zero.wav"},
    ]))
    _, report, _ = _run(tmp_path, frames, dialogues)
    lines = _by_id(report)
    assert lines["Z"]["status"] == "silent"
    assert lines["Z"]["offset_ms"] is None
    assert report["summary"]["checked_lines"] == 1
    assert report["summary"]["mean_offset_ms"] == pytest.approx(5.0, abs=1.0)
    assert "Z" not in [lid for lid in report["summary"]["out_of_tolerance_line_ids"]]


# (6) shared WAV decoded once, two independent conclusions.
def test_shared_wav_decoded_once(tmp_path: Path) -> None:
    write_wav(tmp_path / "wav" / "shared.wav", tone(onset_ms=200.0))
    frames = write_json(tmp_path / "frames.json", make_frames({"A": [210.0], "B": [188.0]}))
    dialogues = write_json(tmp_path / "dialogues.json", make_dialogues([
        {"line_id": "A", "wav_path": "wav/shared.wav"},
        {"line_id": "B", "wav_path": "wav/shared.wav"},
    ]))
    frames_obj = load_frame_table(frames)
    dialogues_obj = load_dialogues(dialogues)
    cache = AudioProbeCache()
    result = verify_all(frames_obj, dialogues_obj, 8.0, cache)
    assert cache.decode_count == 1
    offsets = {line.line_id: line.offset_ms for line in result.lines}
    assert offsets["A"] == pytest.approx(10.0, abs=1.0)
    assert offsets["B"] == pytest.approx(-12.0, abs=1.0)


def test_shared_wav_calls_decoder_once(tmp_path: Path, monkeypatch) -> None:
    """Same check at the decoder-call level, independent of cache bookkeeping."""
    import lip_sync.verify as verify_mod

    write_wav(tmp_path / "wav" / "shared.wav", tone(duration_ms=4000.0, onset_ms=200.0))
    frames = write_json(tmp_path / "frames.json", make_frames({
        "A": [210.0], "B": [188.0], "C": [199.0], "D": [201.0],
    }))
    dialogues = write_json(tmp_path / "dialogues.json", make_dialogues([
        {"line_id": lid, "wav_path": "wav/shared.wav"} for lid in ["A", "B", "C", "D"]
    ]))
    calls: list[str] = []
    real_read = verify_mod.read_wav

    def counting_read(path):
        calls.append(str(path))
        return real_read(path)

    monkeypatch.setattr(verify_mod, "read_wav", counting_read)
    result = verify_all(load_frame_table(frames), load_dialogues(dialogues), 8.0, AudioProbeCache())
    assert len(calls) == 1
    assert len(result.lines) == 4
    assert all(line.status in ("ok", "out_of_tolerance") for line in result.lines)


# (7) unverifiable audio does not disturb other lines.
def test_stereo_wav_is_unverifiable_others_unchanged(tmp_path: Path) -> None:
    write_wav(tmp_path / "wav" / "mono.wav", tone(onset_ms=200.0))
    write_wav(tmp_path / "wav" / "stereo.wav", tone(onset_ms=200.0), channels=2)
    frames = write_json(tmp_path / "frames.json", make_frames({"A": [210.0], "B": [188.0], "S": [210.0]}))
    dialogues = write_json(tmp_path / "dialogues.json", make_dialogues([
        {"line_id": "A", "wav_path": "wav/mono.wav"},
        {"line_id": "B", "wav_path": "wav/mono.wav"},
        {"line_id": "S", "wav_path": "wav/stereo.wav"},
    ]))
    _, report, _ = _run(tmp_path, frames, dialogues)
    lines = _by_id(report)
    assert lines["S"]["status"] == "unverifiable"
    assert lines["S"]["reason"] == "channels_not_mono"
    assert lines["A"]["offset_ms"] == pytest.approx(10.0, abs=1.0)
    assert lines["B"]["offset_ms"] == pytest.approx(-12.0, abs=1.0)


def test_wrong_sample_rate_is_unverifiable(tmp_path: Path) -> None:
    write_wav(tmp_path / "wav" / "48k.wav", tone(sample_rate=48000, onset_ms=200.0), sample_rate=48000)
    write_wav(tmp_path / "wav" / "44k.wav", tone(onset_ms=200.0))
    frames = write_json(tmp_path / "frames.json", make_frames({"A": [205.0], "B": [205.0]}))
    dialogues = write_json(tmp_path / "dialogues.json", make_dialogues([
        {"line_id": "A", "wav_path": "wav/44k.wav"},
        {"line_id": "B", "wav_path": "wav/48k.wav"},
    ]))
    _, report, _ = _run(tmp_path, frames, dialogues)
    lines = _by_id(report)
    assert lines["B"]["status"] == "unverifiable"
    assert lines["B"]["reason"] == "sample_rate_mismatch"
    assert lines["A"]["status"] == "ok"


def test_missing_wav_file_is_unverifiable(tmp_path: Path) -> None:
    write_wav(tmp_path / "wav" / "a.wav", tone(onset_ms=200.0))
    frames = write_json(tmp_path / "frames.json", make_frames({"A": [205.0], "B": [205.0]}))
    dialogues = write_json(tmp_path / "dialogues.json", make_dialogues([
        {"line_id": "A", "wav_path": "wav/a.wav"},
        {"line_id": "B", "wav_path": "wav/does_not_exist.wav"},
    ]))
    _, report, _ = _run(tmp_path, frames, dialogues)
    lines = _by_id(report)
    assert lines["B"]["status"] == "unverifiable"
    assert lines["B"]["reason"] == "missing_wav_file"
    assert lines["A"]["status"] == "ok"


def test_click_only_wav_is_unverifiable_not_silent(tmp_path: Path) -> None:
    click = np.zeros(SAMPLE_RATE, dtype=np.float64)
    click[2000] = 0.9
    write_wav(tmp_path / "wav" / "click.wav", click)
    frames = write_json(tmp_path / "frames.json", make_frames({"C": [205.0]}))
    dialogues = write_json(tmp_path / "dialogues.json", make_dialogues([
        {"line_id": "C", "wav_path": "wav/click.wav"},
    ]))
    _, report, _ = _run(tmp_path, frames, dialogues)
    line = _by_id(report)["C"]
    assert line["status"] == "unverifiable"
    assert line["reason"] == "no_onset_detected"
    assert report["summary"]["silent_lines"] == 0


# (8) output JSON schema and key names.
def test_output_json_schema(tmp_path: Path) -> None:
    write_wav(tmp_path / "wav" / "a.wav", tone(onset_ms=200.0))
    frames = write_json(tmp_path / "frames.json", make_frames({"A": [210.0], "B": [204.0]}))
    dialogues = write_json(tmp_path / "dialogues.json", make_dialogues([
        {"line_id": "A", "wav_path": "wav/a.wav"},
        {"line_id": "B", "wav_path": "wav/a.wav"},
    ]))
    code, report, _ = _run(tmp_path, frames, dialogues)
    assert code == 0
    required = {"line_id", "mouth_time_ms", "onset_ms", "offset_ms", "status"}
    for line in report["lines"]:
        assert required <= set(line)
        assert isinstance(line["line_id"], str)
    lines = _by_id(report)
    assert isinstance(lines["A"]["onset_ms"], float)
    assert isinstance(lines["A"]["offset_ms"], float)
    assert lines["A"]["status"] == "out_of_tolerance"


# (9) ordering: |offset| desc, ties by line_id asc, stable across runs.
def test_ordering_and_determinism(tmp_path: Path) -> None:
    write_wav(tmp_path / "wav" / "a.wav", tone(onset_ms=200.0))
    # offsets: A=+12, B=+2, C=-4, D=-8
    frames = write_json(tmp_path / "frames.json", make_frames({
        "B": [202.0], "A": [212.0], "C": [196.0], "D": [192.0],
    }))
    dialogues = write_json(tmp_path / "dialogues.json", make_dialogues([
        {"line_id": lid, "wav_path": "wav/a.wav"} for lid in ["B", "A", "C", "D"]
    ]))
    _, report1, _ = _run(tmp_path, frames, dialogues)
    order1 = [line["line_id"] for line in report1["lines"]]
    assert order1 == ["A", "D", "C", "B"]
    _, report2, _ = _run(tmp_path, frames, dialogues)
    assert [line["line_id"] for line in report2["lines"]] == order1


def test_ordering_ties_are_lexicographic(tmp_path: Path) -> None:
    write_wav(tmp_path / "wav" / "a.wav", tone(onset_ms=200.0))
    frames = write_json(tmp_path / "frames.json", make_frames({
        "L2": [210.0], "L1": [210.0], "L3": [190.0],
    }))
    dialogues = write_json(tmp_path / "dialogues.json", make_dialogues([
        {"line_id": lid, "wav_path": "wav/a.wav"} for lid in ["L2", "L1", "L3"]
    ]))
    _, report, _ = _run(tmp_path, frames, dialogues)
    assert [line["line_id"] for line in report["lines"]] == ["L1", "L2", "L3"]


# (10) summary with 10, -4, 0 and tolerance 8.
def test_summary_statistics(tmp_path: Path) -> None:
    write_wav(tmp_path / "wav" / "a.wav", tone(onset_ms=200.0))
    frames = write_json(tmp_path / "frames.json", make_frames({
        "A": [210.0], "B": [196.0], "C": [200.0],
    }))
    dialogues = write_json(tmp_path / "dialogues.json", make_dialogues([
        {"line_id": lid, "wav_path": "wav/a.wav"} for lid in ["A", "B", "C"]
    ]))
    _, report, _ = _run(tmp_path, frames, dialogues, tolerance=8.0)
    summary = report["summary"]
    assert summary["checked_lines"] == 3
    assert summary["out_of_tolerance_lines"] == 1
    assert summary["mean_offset_ms"] == pytest.approx(2.0, abs=0.5)
    assert summary["max_offset_ms"] == pytest.approx(10.0, abs=1.0)


# (11) earliest mouth time wins.
def test_earliest_mouth_time_used(tmp_path: Path) -> None:
    write_wav(tmp_path / "wav" / "a.wav", tone(onset_ms=240.0))
    frames = write_json(tmp_path / "frames.json", make_frames({
        "A": [400.0, 320.0, 250.0, 500.0],
    }))
    dialogues = write_json(tmp_path / "dialogues.json", make_dialogues([
        {"line_id": "A", "wav_path": "wav/a.wav"},
    ]))
    _, report, _ = _run(tmp_path, frames, dialogues)
    line = _by_id(report)["A"]
    assert line["mouth_time_ms"] == 250.0
    assert line["offset_ms"] == pytest.approx(10.0, abs=1.0)


# (12) CLI argument errors.
def test_missing_tolerance_exits_nonzero(tmp_path: Path) -> None:
    frames = write_json(tmp_path / "frames.json", make_frames({"A": [200.0]}))
    dialogues = write_json(tmp_path / "dialogues.json", make_dialogues([]))
    import io
    from contextlib import redirect_stderr

    err = io.StringIO()
    with pytest.raises(SystemExit) as excinfo, redirect_stderr(err):
        cli.main(["--frames", str(frames), "--dialogues", str(dialogues), "--out-dir", str(tmp_path / "out")])
    assert excinfo.value.code != 0
    assert "--tolerance-ms" in err.getvalue()


def test_nonexistent_frames_path_exits_nonzero(tmp_path: Path) -> None:
    dialogues = write_json(tmp_path / "dialogues.json", make_dialogues([]))
    import io
    from contextlib import redirect_stderr

    err = io.StringIO()
    with pytest.raises(SystemExit) as excinfo, redirect_stderr(err):
        cli.main([
            "--frames", str(tmp_path / "nope.json"),
            "--dialogues", str(dialogues),
            "--tolerance-ms", "8",
            "--out-dir", str(tmp_path / "out"),
        ])
    assert excinfo.value.code != 0
    assert "--frames" in err.getvalue()


def test_invalid_json_exits_with_code_3(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    dialogues = write_json(tmp_path / "dialogues.json", make_dialogues([]))
    import io
    from contextlib import redirect_stderr

    err = io.StringIO()
    with redirect_stderr(err):
        code = cli.main([
            "--frames", str(bad), "--dialogues", str(dialogues),
            "--tolerance-ms", "8", "--out-dir", str(tmp_path / "out"),
        ])
    assert code == 3
    assert "JSON" in err.getvalue()


# (13) empty dialogue list still writes a report.
def test_empty_dialogue_list_writes_report(tmp_path: Path) -> None:
    frames = write_json(tmp_path / "frames.json", make_frames({}))
    dialogues = write_json(tmp_path / "dialogues.json", make_dialogues([]))
    code, report, _ = _run(tmp_path, frames, dialogues)
    assert code == 0
    assert report["lines"] == []
    assert report["summary"]["no_valid_lines"] is True
    assert report["summary"]["checked_lines"] == 0
    assert "message" in report["summary"]


def test_all_lines_unverifiable_still_reports(tmp_path: Path) -> None:
    frames = write_json(tmp_path / "frames.json", make_frames({"A": [200.0]}))
    dialogues = write_json(tmp_path / "dialogues.json", make_dialogues([
        {"line_id": "A", "wav_path": "wav/none.wav"},
    ]))
    code, report, _ = _run(tmp_path, frames, dialogues)
    assert code == 0
    assert report["summary"]["checked_lines"] == 0
    assert report["summary"]["no_valid_lines"] is True
    assert _by_id(report)["A"]["status"] == "unverifiable"


# Invalid frame entries must not sink the other lines.
def test_invalid_frame_entries_isolated(tmp_path: Path) -> None:
    write_wav(tmp_path / "wav" / "a.wav", tone(onset_ms=200.0))
    frames = write_json(tmp_path / "frames.json", {"frames": [
        {"line_id": "A", "mouth_time_ms": 205.0},
        {"line_id": "B", "mouth_time_ms": "not-a-number"},
        {"mouth_time_ms": 100.0},
        {"line_id": "C", "mouth_time_ms": -5.0},
        {"line_id": "D", "mouth_time_ms": 205.0},
    ]})
    dialogues = write_json(tmp_path / "dialogues.json", make_dialogues([
        {"line_id": lid, "wav_path": "wav/a.wav"} for lid in ["A", "B", "C", "D"]
    ]))
    _, report, _ = _run(tmp_path, frames, dialogues)
    lines = _by_id(report)
    assert lines["A"]["status"] == "ok"
    assert lines["D"]["status"] == "ok"
    assert lines["B"]["status"] == "pending"
    assert lines["C"]["status"] == "pending"
    assert len(report["input_issues"]) >= 2


def test_console_output_contains_summary(tmp_path: Path) -> None:
    write_wav(tmp_path / "wav" / "a.wav", tone(onset_ms=200.0))
    frames = write_json(tmp_path / "frames.json", make_frames({"A": [210.0]}))
    dialogues = write_json(tmp_path / "dialogues.json", make_dialogues([
        {"line_id": "A", "wav_path": "wav/a.wav"},
    ]))
    _, _, text = _run(tmp_path, frames, dialogues)
    assert "A" in text
    assert "超差" in text
    assert "结论已写入" in text
