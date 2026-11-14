import json
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "loop_seams.py"
SR = 44100
WINDOW = 64


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def wav_frames(path: Path) -> tuple[int, int, int, int]:
    with wave.open(str(path), "rb") as w:
        return w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()


def write_wav(path: Path, samples, channels=1, rate=SR, width=2) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = np.asarray(samples)
    if data.ndim == 2:
        packed = data.astype("<i2").reshape(-1)
    else:
        packed = data.astype("<i2")
    with wave.open(str(path), "wb") as out:
        out.setnchannels(channels)
        out.setsampwidth(width)
        out.setframerate(rate)
        out.writeframes(packed.tobytes())


def seam_audio(seed=0, duration_ms=1000, match_ms=None, bad_ms=(), amplitude=6000):
    frames = round(duration_ms * SR / 1000)
    rng = np.random.default_rng(seed)
    data = (rng.standard_normal(frames) * amplitude).clip(-32768, 32767).astype("<i2")
    head = data[:WINDOW].copy()
    head[0] = head[-1]
    data[:WINDOW] = head
    for ms in bad_ms:
        i = round(ms * SR / 1000)
        data[i - WINDOW : i] = -head
    if match_ms is not None:
        i = round(match_ms * SR / 1000)
        data[i - WINDOW : i] = head
    return data


def run_tool(manifest: Path, *extra, cwd=None):
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(manifest), *extra],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(cwd or ROOT),
    )


def make_manifest(tmp_path: Path, entries, output_dir="out", audio_dir="audio"):
    payload = {
        "output_dir": output_dir,
        "audio_dir": audio_dir,
        "entries": entries,
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def load_report(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "out" / "loop_report.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 1-2: manifest parsing and validation
# ---------------------------------------------------------------------------


def test_missing_expected_loop_length_is_invalid(tmp_path):
    write_wav(tmp_path / "audio" / "a.wav", seam_audio(match_ms=500))
    manifest = make_manifest(
        tmp_path,
        [{"filename": "a.wav", "candidate_seams_ms": [500]}],
    )
    proc = run_tool(manifest)
    assert proc.returncode == 0, proc.stderr
    entry = load_report(tmp_path)["entries"][0]
    assert entry["status"] == "invalid"
    assert entry["expected_loop_ms"] is None
    assert entry["exported_frames"] is None
    assert entry["selected_seam_ms"] is None
    assert not list((tmp_path / "out").glob("*.wav"))


@pytest.mark.parametrize(
    "mutation, code",
    [
        ({"filename": 5}, "invalid_filename_type"),
        ({"filename": ""}, "empty_filename"),
        ({"expected_loop_ms": "800"}, "invalid_expected_loop_ms_type"),
        ({"expected_loop_ms": 0}, "invalid_expected_loop_ms_value"),
        ({"expected_loop_ms": -10}, "invalid_expected_loop_ms_value"),
        ({"expected_loop_ms": 1.5}, "invalid_expected_loop_ms_type"),
        ({"expected_loop_ms": True}, "invalid_expected_loop_ms_type"),
        ({"candidate_seams_ms": "500"}, "invalid_candidate_seams_type"),
        ({"candidate_seams_ms": []}, "empty_candidate_seams"),
        ({"candidate_seams_ms": [500, 1.5]}, "invalid_candidate_seam_type"),
        ({"candidate_seams_ms": [500, -1]}, "negative_candidate_seam"),
        ({"candidate_seams_ms": [500, True]}, "invalid_candidate_seam_type"),
    ],
)
def test_type_and_value_errors_are_invalid(tmp_path, mutation, code):
    write_wav(tmp_path / "audio" / "a.wav", seam_audio(match_ms=500))
    entry = {
        "filename": "a.wav",
        "expected_loop_ms": 600,
        "candidate_seams_ms": [500],
    }
    entry.update(mutation)
    manifest = make_manifest(tmp_path, [entry])
    proc = run_tool(manifest)
    assert proc.returncode == 0, proc.stderr
    report_entry = load_report(tmp_path)["entries"][0]
    assert report_entry["status"] == "invalid"
    assert code in {e["code"] for e in report_entry["validation_errors"]}


def test_out_of_range_seam_is_invalid_and_not_exported(tmp_path):
    write_wav(tmp_path / "audio" / "a.wav", seam_audio(duration_ms=1000, match_ms=300))
    write_wav(tmp_path / "audio" / "b.wav", seam_audio(seed=2, duration_ms=1000, match_ms=300))
    manifest = make_manifest(
        tmp_path,
        [
            {"filename": "a.wav", "expected_loop_ms": 500, "candidate_seams_ms": [300, 1500]},
            {"filename": "b.wav", "expected_loop_ms": 500, "candidate_seams_ms": [300]},
        ],
    )
    proc = run_tool(manifest)
    assert proc.returncode == 0, proc.stderr
    entries = load_report(tmp_path)["entries"]
    assert entries[0]["status"] == "invalid"
    assert entries[0]["reason"] == "candidate_seam_out_of_range"
    assert entries[0]["exported_frames"] is None
    assert entries[1]["status"] == "exported"
    # Only the valid entry produced audio.
    assert len(list((tmp_path / "out").glob("*.wav"))) == 1


# ---------------------------------------------------------------------------
# 3-5: seam selection and export
# ---------------------------------------------------------------------------


def test_selects_smoothest_candidate_and_exports_matching_frames(tmp_path):
    samples = seam_audio(seed=3, duration_ms=1000, match_ms=750, bad_ms=(500, 880))
    write_wav(tmp_path / "audio" / "a.wav", samples)
    manifest = make_manifest(
        tmp_path,
        [{"filename": "a.wav", "expected_loop_ms": 800, "candidate_seams_ms": [500, 750, 880]}],
    )
    proc = run_tool(manifest)
    assert proc.returncode == 0, proc.stderr
    entry = load_report(tmp_path)["entries"][0]
    assert entry["status"] == "exported"
    assert entry["selected_seam_ms"] == 750
    expected_frames = round(750 * SR / 1000)
    assert entry["exported_frames"] == expected_frames
    out = tmp_path / "out" / entry["exported_file"]
    rate, channels, width, frames = wav_frames(out)
    assert (rate, channels, width) == (SR, 1, 2)
    assert frames == expected_frames
    with wave.open(str(out), "rb") as w:
        exported = np.frombuffer(w.readframes(frames), dtype="<i2")
    # A seamless loop: the exported tail lines up with the exported head.
    assert exported[-1] == exported[0]
    assert np.mean(np.abs(exported[:WINDOW].astype(int) - exported[-WINDOW:].astype(int))) < 1


def test_report_fields_and_order(tmp_path):
    write_wav(tmp_path / "audio" / "first.wav", seam_audio(seed=4, match_ms=400))
    write_wav(tmp_path / "audio" / "second.wav", seam_audio(seed=5, match_ms=300))
    manifest = make_manifest(
        tmp_path,
        [
            {"filename": "first.wav", "expected_loop_ms": 600, "candidate_seams_ms": [400]},
            {"filename": "second.wav", "expected_loop_ms": 500, "candidate_seams_ms": [300]},
        ],
    )
    proc = run_tool(manifest)
    assert proc.returncode == 0, proc.stderr
    report = load_report(tmp_path)
    assert [e["filename"] for e in report["entries"]] == ["first.wav", "second.wav"]
    first = report["entries"][0]
    assert first["expected_loop_ms"] == 600
    assert first["selected_seam_ms"] == 400
    assert first["exported_frames"] == round(400 * SR / 1000)
    assert first["status"] == "exported"


# ---------------------------------------------------------------------------
# 6-10: failure classification
# ---------------------------------------------------------------------------


def test_three_way_classification(tmp_path):
    write_wav(tmp_path / "audio" / "ok.wav", seam_audio(seed=6, match_ms=300))
    manifest = make_manifest(
        tmp_path,
        [
            {"filename": "ok.wav", "expected_loop_ms": 500, "candidate_seams_ms": [300]},
            {"filename": "absent.wav", "expected_loop_ms": 500, "candidate_seams_ms": [300]},
            {"filename": "ok.wav", "expected_loop_ms": 500, "candidate_seams_ms": [4000]},
        ],
    )
    proc = run_tool(manifest)
    assert proc.returncode == 0, proc.stderr
    report = load_report(tmp_path)
    assert [e["status"] for e in report["entries"]] == ["exported", "incomplete", "invalid"]
    assert report["summary"]["exported"] == 1
    assert report["summary"]["incomplete"] == 1
    assert report["summary"]["invalid"] == 1
    # Incomplete values stay null rather than being reported as zero.
    absent = report["entries"][1]
    assert absent["selected_seam_ms"] is None
    assert absent["exported_frames"] is None


def test_missing_file_does_not_block_others(tmp_path):
    write_wav(tmp_path / "audio" / "a.wav", seam_audio(seed=7, match_ms=300))
    write_wav(tmp_path / "audio" / "b.wav", seam_audio(seed=8, match_ms=200))
    manifest = make_manifest(
        tmp_path,
        [
            {"filename": "a.wav", "expected_loop_ms": 500, "candidate_seams_ms": [300]},
            {"filename": "gone.wav", "expected_loop_ms": 500, "candidate_seams_ms": [300]},
            {"filename": "b.wav", "expected_loop_ms": 400, "candidate_seams_ms": [200]},
        ],
    )
    proc = run_tool(manifest)
    assert proc.returncode == 0, proc.stderr
    report = load_report(tmp_path)
    assert [e["status"] for e in report["entries"]] == ["exported", "incomplete", "exported"]
    assert len(list((tmp_path / "out").glob("*.wav"))) == 2


def test_non_wav_and_wrong_format_are_incomplete(tmp_path):
    audio = tmp_path / "audio"
    audio.mkdir(parents=True)
    (audio / "fake.wav").write_bytes(b"not a wav file at all")
    write_wav(
        audio / "stereo.wav",
        np.column_stack([seam_audio(seed=9, match_ms=300)] * 2),
        channels=2,
    )
    write_wav(audio / "rate.wav", seam_audio(seed=10, match_ms=300), rate=48000)
    write_wav(audio / "good.wav", seam_audio(seed=11, match_ms=300))
    manifest = make_manifest(
        tmp_path,
        [
            {"filename": "fake.wav", "expected_loop_ms": 500, "candidate_seams_ms": [300]},
            {"filename": "stereo.wav", "expected_loop_ms": 500, "candidate_seams_ms": [300]},
            {"filename": "rate.wav", "expected_loop_ms": 500, "candidate_seams_ms": [300]},
            {"filename": "good.wav", "expected_loop_ms": 500, "candidate_seams_ms": [300]},
        ],
    )
    proc = run_tool(manifest)
    assert proc.returncode == 0, proc.stderr
    report = load_report(tmp_path)
    assert [e["status"] for e in report["entries"]] == [
        "incomplete",
        "incomplete",
        "incomplete",
        "exported",
    ]
    assert len(list((tmp_path / "out").glob("*.wav"))) == 1


def test_audio_shorter_than_expected_is_incomplete_and_keeps_expected(tmp_path):
    write_wav(tmp_path / "audio" / "short.wav", seam_audio(seed=12, duration_ms=600, match_ms=300))
    manifest = make_manifest(
        tmp_path,
        [{"filename": "short.wav", "expected_loop_ms": 900, "candidate_seams_ms": [300]}],
    )
    proc = run_tool(manifest)
    assert proc.returncode == 0, proc.stderr
    entry = load_report(tmp_path)["entries"][0]
    assert entry["status"] == "incomplete"
    assert entry["reason"] == "audio_too_short"
    assert entry["expected_loop_ms"] == 900
    assert not list((tmp_path / "out").glob("*.wav"))


def test_silent_audio_is_not_exported(tmp_path):
    write_wav(tmp_path / "audio" / "silent.wav", np.zeros(SR, dtype="<i2"))
    write_wav(tmp_path / "audio" / "good.wav", seam_audio(seed=13, match_ms=300))
    manifest = make_manifest(
        tmp_path,
        [
            {"filename": "silent.wav", "expected_loop_ms": 500, "candidate_seams_ms": [300, 400]},
            {"filename": "good.wav", "expected_loop_ms": 500, "candidate_seams_ms": [300]},
        ],
    )
    proc = run_tool(manifest)
    assert proc.returncode == 0, proc.stderr
    report = load_report(tmp_path)
    silent = report["entries"][0]
    assert silent["status"] == "incomplete"
    assert silent["reason"] == "silent_audio"
    assert report["entries"][1]["status"] == "exported"
    exported_files = {e["exported_file"] for e in report["entries"] if e["exported_file"]}
    assert exported_files == {"good__loop_002.wav"}


# ---------------------------------------------------------------------------
# 11-12: independence
# ---------------------------------------------------------------------------


def test_shared_source_different_expected_lengths_are_independent(tmp_path):
    write_wav(
        tmp_path / "audio" / "shared.wav",
        seam_audio(seed=14, duration_ms=1200, match_ms=300, bad_ms=(700,)),
    )
    manifest = make_manifest(
        tmp_path,
        [
            {"filename": "shared.wav", "expected_loop_ms": 400, "candidate_seams_ms": [300]},
            {"filename": "shared.wav", "expected_loop_ms": 800, "candidate_seams_ms": [700]},
        ],
    )
    proc = run_tool(manifest)
    assert proc.returncode == 0, proc.stderr
    report = load_report(tmp_path)
    assert all(e["status"] == "exported" for e in report["entries"])
    frames = [e["exported_frames"] for e in report["entries"]]
    assert frames == [round(300 * SR / 1000), round(700 * SR / 1000)]
    exports = [tmp_path / "out" / e["exported_file"] for e in report["entries"]]
    assert exports[0] != exports[1]
    assert all(path.is_file() for path in exports)
    assert wav_frames(exports[0])[3] != wav_frames(exports[1])[3]


def test_shared_candidates_different_sources_are_independent(tmp_path):
    write_wav(tmp_path / "audio" / "a.wav", seam_audio(seed=15, match_ms=300, bad_ms=(420,)))
    write_wav(tmp_path / "audio" / "b.wav", seam_audio(seed=16, match_ms=420, bad_ms=(300,)))
    manifest = make_manifest(
        tmp_path,
        [
            {"filename": "a.wav", "expected_loop_ms": 500, "candidate_seams_ms": [300, 420]},
            {"filename": "b.wav", "expected_loop_ms": 500, "candidate_seams_ms": [300, 420]},
        ],
    )
    proc = run_tool(manifest)
    assert proc.returncode == 0, proc.stderr
    report = load_report(tmp_path)
    assert [e["selected_seam_ms"] for e in report["entries"]] == [300, 420]
    assert [e["exported_frames"] for e in report["entries"]] == [
        round(300 * SR / 1000),
        round(420 * SR / 1000),
    ]


# ---------------------------------------------------------------------------
# 13-15: fatal errors, reruns, summary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {"audio_dir": "audio", "entries": []},
        {"output_dir": "", "entries": []},
        {"output_dir": "   ", "entries": []},
        {"output_dir": 5, "entries": []},
        {"output_dir": "out", "entries": {"filename": "x"}},
        {"output_dir": "out"},
        [{"output_dir": "out"}],
    ],
)
def test_invalid_manifest_exits_nonzero_without_output(tmp_path, payload):
    write_wav(tmp_path / "audio" / "a.wav", seam_audio(match_ms=300))
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    proc = run_tool(path)
    assert proc.returncode != 0
    assert proc.stderr.strip()
    assert not (tmp_path / "out").exists()


def test_malformed_json_exits_nonzero(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text("{ this is not json", encoding="utf-8")
    proc = run_tool(path)
    assert proc.returncode != 0
    assert "JSON" in proc.stderr


def test_output_dir_created_and_rerun_is_stable(tmp_path):
    write_wav(tmp_path / "audio" / "a.wav", seam_audio(seed=17, match_ms=300))
    write_wav(tmp_path / "audio" / "b.wav", seam_audio(seed=18, match_ms=200))
    manifest = make_manifest(
        tmp_path,
        [
            {"filename": "a.wav", "expected_loop_ms": 500, "candidate_seams_ms": [300]},
            {"filename": "b.wav", "expected_loop_ms": 400, "candidate_seams_ms": [200]},
        ],
    )
    assert not (tmp_path / "out").exists()
    first = run_tool(manifest)
    first_files = sorted(p.name for p in (tmp_path / "out").glob("*.wav"))
    assert first.returncode == 0, first.stderr
    assert len(first_files) == 2
    first_contents = {name: (tmp_path / "out" / name).read_bytes() for name in first_files}

    second = run_tool(manifest)
    assert second.returncode == 0, second.stderr
    assert sorted(p.name for p in (tmp_path / "out").glob("*.wav")) == first_files
    for name, content in first_contents.items():
        assert (tmp_path / "out" / name).read_bytes() == content


def test_summary_matches_report_counts(tmp_path):
    write_wav(tmp_path / "audio" / "ok.wav", seam_audio(seed=19, match_ms=300))
    manifest = make_manifest(
        tmp_path,
        [
            {"filename": "ok.wav", "expected_loop_ms": 500, "candidate_seams_ms": [300]},
            {"filename": "gone.wav", "expected_loop_ms": 500, "candidate_seams_ms": [300]},
            {"filename": "ok.wav", "expected_loop_ms": 500, "candidate_seams_ms": [9999]},
        ],
    )
    proc = run_tool(manifest)
    assert proc.returncode == 0, proc.stderr
    report = load_report(tmp_path)
    summary = report["summary"]
    assert summary["total"] == 3
    assert summary["exported"] == 1
    assert summary["incomplete"] == 1
    assert summary["invalid"] == 1
    assert "完整导出 1" in proc.stdout
    assert "未完成 1" in proc.stdout
    assert "无效 1" in proc.stdout


# ---------------------------------------------------------------------------
# Additional boundary cases
# ---------------------------------------------------------------------------


def test_non_object_entry_is_invalid(tmp_path):
    manifest = make_manifest(tmp_path, ["not an entry", 42, None])
    proc = run_tool(manifest)
    assert proc.returncode == 0, proc.stderr
    entries = load_report(tmp_path)["entries"]
    assert [e["status"] for e in entries] == ["invalid"] * 3
    assert all(e["reason"] == "entry_not_object" for e in entries)


def test_only_zero_candidate_is_incomplete(tmp_path):
    write_wav(tmp_path / "audio" / "a.wav", seam_audio(seed=21, match_ms=300))
    manifest = make_manifest(
        tmp_path,
        [{"filename": "a.wav", "expected_loop_ms": 500, "candidate_seams_ms": [0]}],
    )
    proc = run_tool(manifest)
    assert proc.returncode == 0, proc.stderr
    entry = load_report(tmp_path)["entries"][0]
    assert entry["status"] == "incomplete"
    assert entry["reason"] == "zero_length_loop"
    assert entry["exported_frames"] is None
    assert not list((tmp_path / "out").glob("*.wav"))


def test_seam_exactly_at_audio_end_is_accepted(tmp_path):
    samples = seam_audio(seed=22, duration_ms=500, match_ms=500)
    write_wav(tmp_path / "audio" / "a.wav", samples)
    manifest = make_manifest(
        tmp_path,
        [{"filename": "a.wav", "expected_loop_ms": 500, "candidate_seams_ms": [250, 500]}],
    )
    proc = run_tool(manifest)
    assert proc.returncode == 0, proc.stderr
    entry = load_report(tmp_path)["entries"][0]
    assert entry["status"] == "exported"
    assert entry["selected_seam_ms"] == 500
    assert entry["exported_frames"] == len(samples)


def test_truncated_wav_payload_is_incomplete(tmp_path):
    audio = tmp_path / "audio"
    write_wav(audio / "a.wav", seam_audio(seed=23, match_ms=300))
    payload = (audio / "a.wav").read_bytes()
    # Keep a valid header but cut the sample data short.
    (audio / "trunc.wav").write_bytes(payload[: len(payload) // 2])
    manifest = make_manifest(
        tmp_path,
        [{"filename": "trunc.wav", "expected_loop_ms": 500, "candidate_seams_ms": [300]}],
    )
    proc = run_tool(manifest)
    assert proc.returncode == 0, proc.stderr
    entry = load_report(tmp_path)["entries"][0]
    assert entry["status"] == "incomplete"
    assert entry["reason"] in {"decode_error", "truncated_audio", "audio_too_short"}


def test_entries_order_preserved_with_mixed_statuses(tmp_path):
    write_wav(tmp_path / "audio" / "a.wav", seam_audio(seed=24, match_ms=300))
    manifest = make_manifest(
        tmp_path,
        [
            {"filename": "a.wav", "expected_loop_ms": 500, "candidate_seams_ms": [9999]},
            {"filename": "nope.wav", "expected_loop_ms": 500, "candidate_seams_ms": [300]},
            {"filename": "a.wav", "expected_loop_ms": 500, "candidate_seams_ms": [300]},
            {"filename": "a.wav", "candidate_seams_ms": [300]},
        ],
    )
    proc = run_tool(manifest)
    assert proc.returncode == 0, proc.stderr
    entries = load_report(tmp_path)["entries"]
    assert [e["index"] for e in entries] == [0, 1, 2, 3]
    assert [e["status"] for e in entries] == [
        "invalid",
        "incomplete",
        "exported",
        "invalid",
    ]
    for entry in entries:
        for key in ("selected_seam_ms", "exported_frames", "exported_file"):
            if entry["status"] != "exported":
                assert entry[key] is None


# ---------------------------------------------------------------------------
# Unit checks for the seam scorer
# ---------------------------------------------------------------------------


def test_seam_window_exact_match_scores_zero():
    sys.path.insert(0, str(ROOT))
    import loop_seams

    data = seam_audio(seed=20, duration_ms=1000, match_ms=500)
    assert loop_seams.seam_difference(data, round(500 * SR / 1000)) == 0.0
    assert loop_seams.seam_difference(data, round(400 * SR / 1000)) > 100
    assert loop_seams.seam_difference(data, 0) is None
    assert loop_seams.seam_difference(data, len(data) + 1) is None
