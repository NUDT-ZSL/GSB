"""Built-in deterministic regression suite for all twelve requirements.

The suite constructs every sample at runtime in a temporary directory, invokes
the real CLI through subprocesses where exit codes/stderr matter, and performs
in-process assertions for pure logic.  ``python -m mvst selftest`` exits 0 only
when every check passes; failures are reported without leaking tracebacks to
the CLI contract (the selftest itself prints diagnostic details).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import time
from fractions import Fraction
from pathlib import Path

from . import container
from .container import (
    BLOCK_HEADER_SIZE, HEADER_STRUCT, MAGIC,
    TYPE_AUDIO, extract_block, read_master_header,
)
from .manifest import load_manifest
from .pipeline import REPORT_KEY_ORDER
from .resampler import resample_track
from .samplegen import (
    build_srt, build_standard_suite, build_video, build_wav,
    sine_samples, standard_manifest, write_manifest, write_wav,
)
from .subtitles import parse_srt, snap_and_resolve
from .timebase import ms_to_frame, ms_to_master_sample

_PY = sys.executable
_results: list[tuple[str, bool, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    _results.append((name, bool(condition), detail))
    marker = "PASS" if condition else "FAIL"
    print(f"[{marker}] {name}" + (f" - {detail}" if detail and not condition else ""))


def _run_cli(*argv: str, cwd: str | Path, env: dict | None = None,
             input_bytes: bytes | None = None) -> tuple[int, str, str]:
    merged_env = dict(os.environ)
    if env:
        merged_env.update(env)
    proc = subprocess.run(
        [_PY, "-m", "mvst", *argv],
        cwd=str(cwd), env=merged_env, capture_output=True, text=True,
        input=input_bytes, timeout=120,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _json_line(stderr: str) -> dict:
    lines = [line for line in stderr.strip().splitlines() if line.strip()]
    assert len(lines) == 1, f"expected one stderr line, got: {stderr!r}"
    return json.loads(lines[0])


# --------------------------------------------------------------------------
# Requirement 1: WAV parsing
# --------------------------------------------------------------------------

def test_wav(base: Path) -> None:
    d = base / "t1_wav"
    d.mkdir()
    stereo = d / "stereo.wav"
    eight_bit = d / "eight.wav"
    overflow = d / "overflow.wav"
    good = d / "good.wav"
    write_wav(stereo, 48000, [100] * 100, channels=2)
    write_wav(eight_bit, 48000, [10] * 100, bits=8)
    write_wav(overflow, 48000, [10] * 100, bad_fmt_length=True)
    write_wav(good, 44100, [100] * 44100)

    # Build a minimal valid manifest so validate reaches WAV parsing.
    for bad in (stereo, eight_bit, overflow):
        manifest = {
            "master_sample_rate": 48000, "video_fps": 25,
            "video_file": "video.raw", "languages": ["en"],
            "tracks": [{"language": "en", "offset_ms": 0, "gain_db": 0.0,
                        "file": bad.name}],
            "subtitles": [],
        }
        write_manifest(d / "manifest.json", manifest)
        (d / "video.raw").write_bytes(build_video(25))
        code, _out, err = _run_cli("validate", "--manifest", "manifest.json",
                                   "--root", ".", cwd=d)
        payload = _json_line(err)
        check(f"wav reject {bad.name}", code == 2
              and payload["error_code"] == "E_WAV_FORMAT"
              and payload["file"].endswith(bad.name),
              f"code={code} err={err!r}")

    manifest["tracks"][0]["file"] = "good.wav"
    write_manifest(d / "manifest.json", manifest)
    code, out, err = _run_cli("validate", "--manifest", "manifest.json",
                              "--root", ".", cwd=d)
    report = json.loads(out)
    audio = report["audio"][0]
    check("wav good exit 0", code == 0, err)
    check("wav source_samples 44100", audio["source_samples"] == 44100)
    check("wav duration exactly 1",
          audio["duration_sec"] == {"numerator": 1, "denominator": 1})
    check("wav no traceback", "Traceback" not in err)

    # Odd-length chunk padding path (data chunk with odd size is itself
    # invalid for 16-bit; instead validate a trailing odd LIST-like chunk via
    # raw construction with even data plus odd padding byte).
    blob = build_wav(48000, [1, 2, 3, 4])
    # Inject an odd-sized ancillary chunk before data to exercise padding.
    anc = b"LIST" + struct.pack("<I", 3) + b"abc" + b"\x00"
    riff_body = blob[12:]
    fmt_end = riff_body.index(b"data")
    rebuilt = (b"RIFF" + struct.pack("<I", 4 + len(riff_body[:fmt_end])
                                     + len(anc) + len(riff_body[fmt_end:]))
               + b"WAVE" + riff_body[:fmt_end] + anc + riff_body[fmt_end:])
    (d / "padded.wav").write_bytes(rebuilt)
    from .wavfile import parse_wav
    parsed = parse_wav(d / "padded.wav")
    check("wav odd chunk padding", parsed.source_samples == 4)


# --------------------------------------------------------------------------
# Requirement 2: manifest semantic validation
# --------------------------------------------------------------------------

def test_manifest(base: Path) -> None:
    d = base / "t2_manifest"
    d.mkdir()
    (d / "video.raw").write_bytes(build_video(25))
    template = {
        "master_sample_rate": 48000, "video_fps": 25,
        "video_file": "video.raw", "languages": ["en"],
        "tracks": [{"language": "en", "offset_ms": 0, "gain_db": 0.0,
                    "file": "a.wav"}],
        "subtitles": [],
    }

    bad_offset = json.loads(json.dumps(template))
    bad_offset["tracks"][0]["offset_ms"] = 6000
    write_manifest(d / "manifest.json", bad_offset)
    code, _out, err = _run_cli("validate", "--manifest", "manifest.json",
                               "--root", ".", cwd=d)
    payload = _json_line(err)
    check("manifest offset range", code == 2
          and payload["error_code"] == "E_MANIFEST"
          and payload["pointer"] == "/tracks/0/offset_ms", err)

    bad_lang = json.loads(json.dumps(template))
    bad_lang["tracks"][0]["language"] = "de"
    write_manifest(d / "manifest.json", bad_lang)
    code, _out, err = _run_cli("validate", "--manifest", "manifest.json",
                               "--root", ".", cwd=d)
    payload = _json_line(err)
    check("manifest language whitelist", code == 2
          and payload["pointer"] == "/tracks/0/language", err)

    fixed = json.loads(json.dumps(template))
    fixed["tracks"][0]["offset_ms"] = 123
    write_manifest(d / "manifest.json", fixed)
    write_wav(d / "a.wav", 48000, [0] * 480)
    code, _out, err = _run_cli("validate", "--manifest", "manifest.json",
                               "--root", ".", cwd=d)
    check("manifest fixed valid", code == 0, err)


# --------------------------------------------------------------------------
# Requirement 3: unified rational time base
# --------------------------------------------------------------------------

def test_timebase() -> None:
    check("timebase 123ms -> 5904", ms_to_master_sample(123) == 5904,
          str(ms_to_master_sample(123)))
    check("timebase 40ms -> frame 1", ms_to_frame(40) == 1)
    check("timebase 80ms -> frame 2", ms_to_frame(80) == 2)
    check("timebase exact rational",
          Fraction(123 * 48000, 1000) == Fraction(5904, 1))
    check("timebase no float tail",
          str(ms_to_master_sample(123)) == "5904")


def test_timebase_report(base: Path) -> None:
    d = base / "t3_timebase"
    build_standard_suite(d, gains={"en": 0.0, "es": 0.0, "fr": 0.0, "ja": 0.0})
    manifest = load_manifest(d / "manifest.json")
    raw = manifest.raw
    raw["tracks"][0]["offset_ms"] = 123
    write_manifest(d / "manifest.json", raw)
    code, out, err = _run_cli("validate", "--manifest", "manifest.json",
                              "--root", ".", cwd=d)
    report = json.loads(out)
    check("timebase report exit", code == 0, err)
    offsets = {item["language"]: item for item
               in report["intermediate"]["audio_offsets"]}
    check("timebase report 5904", offsets["en"]["start_sample"] == 5904)
    check("timebase report integer type",
          isinstance(offsets["en"]["start_sample"], int))
    check("timebase no float tokens", "5903.999" not in out
          and "5904.0" not in out)
    # Subtitle ms->frame snapshots in the shared intermediate section: the
    # standard suite's first cue is (0,40)ms -> frames 0..1 and its end of
    # 40ms must snap to frame 1; an 80ms boundary snaps to frame 2.
    snaps = report["intermediate"]["subtitle_frame_snaps"]
    first = next(s for s in snaps if s["seq"] == 1 and s["track"] == 0)
    check("timebase sub 40ms -> frame 1", first["end_frame"] == 1, str(first))
    check("timebase sub start frame 0", first["start_frame"] == 0)
    eighty = [s for s in snaps if s["end_frame"] == 2 and s["track"] == 0]
    check("timebase sub 80ms -> frame 2", len(eighty) >= 1, str(snaps[:6]))


# --------------------------------------------------------------------------
# Requirement 4: resampling + gain + determinism
# --------------------------------------------------------------------------

def test_resampler() -> None:
    samples = sine_samples(44100, 1.0, 0.9)
    r1 = resample_track(0, samples, 44100, 6.0)
    r2 = resample_track(0, samples, 44100, 6.0)
    check("resample length window", 47999 <= r1.resampled_samples <= 48001,
          str(r1.resampled_samples))
    check("resample source 44100", r1.source_samples == 44100)
    check("resample clip > 0", r1.clip_count > 0, str(r1.clip_count))
    check("resample reproducible flag", r1.reproducible is True)
    check("resample bit identical", r1.samples == r2.samples
          and r1.peak == r2.peak and r1.rms == r2.rms)
    check("resample exact rational recorded",
          r1.exact_master_samples == Fraction(48000, 1))


def test_package_determinism(base: Path) -> None:
    d = base / "t4_resample"
    build_standard_suite(d)
    code, out1, err = _run_cli("package", "--manifest", "manifest.json",
                               "--root", ".", "--out", "m1.mvst",
                               "--force", cwd=d)
    check("package exit 0", code == 0, err)
    code, out2, _err = _run_cli("package", "--manifest", "manifest.json",
                                "--root", ".", "--out", "m2.mvst",
                                "--force", cwd=d)
    r1, r2 = json.loads(out1), json.loads(out2)
    r1.pop("generated_at"), r2.pop("generated_at")
    check("package report deterministic", r1 == r2)
    check("package bytes deterministic",
          hashlib.sha256((d / "m1.mvst").read_bytes()).digest()
          == hashlib.sha256((d / "m2.mvst").read_bytes()).digest())
    audio = r1["audio"][0]
    check("package resampled window",
          47999 <= audio["resampled_samples"] <= 48001)
    clipped = next(a for a in r1["audio"] if a["language"] == "ja")
    check("package clip count", clipped["clip_count"] > 0,
          str(clipped["clip_count"]))
    check("package reproducibility block",
          r1["reproducibility"]["reproducible"] is True)


# --------------------------------------------------------------------------
# Requirement 5: subtitle snapping and conflict resolution
# --------------------------------------------------------------------------

def test_subtitles() -> None:
    cues = [(1, 0, 40, "a"), (2, 40, 80, "b"), (3, 80, 120, "c")]
    raw = parse_srt_text(build_srt(cues, bom=True, crlf=True))
    result = snap_and_resolve(raw, "<mem>")
    adjusted = [c.adjusted for c in result.cues]
    check("srt adjusted flags", adjusted == [False, True, False], str(adjusted))
    check("srt frames 0-1",
          (result.cues[0].new_start_frame, result.cues[0].new_end_frame)
          == (0, 1))
    check("srt frames 2-3",
          (result.cues[1].new_start_frame, result.cues[1].new_end_frame)
          == (2, 3))
    check("srt frames 4-5",
          (result.cues[2].new_start_frame, result.cues[2].new_end_frame)
          == (4, 5))

    # Overtake case: cue 2 pushed past cue 3's raw start -> cue 2 dropped.
    overtake = [(1, 0, 120, "long"), (2, 60, 90, "mid"), (3, 70, 100, "next")]
    raw2 = parse_srt_text(build_srt(overtake))
    res2 = snap_and_resolve(raw2, "<mem>")
    codes = [w["error_code"] for w in res2.warnings]
    dropped = [c.seq for c in res2.cues if c.dropped]
    check("srt overtake dropped", "E_SUB_OVERLAP" in codes and 2 in dropped,
          f"{codes} {dropped}")

    # Bad time format on one cue must not kill the batch.
    bad = build_srt(cues, bad_time_seq=2)
    raw3 = parse_srt_text(bad)
    res3 = snap_and_resolve(raw3, "<mem>")
    check("srt format error recorded",
          any(e["error_code"] == "E_SUB_FORMAT" for e in res3.format_errors))
    check("srt remaining cues processed",
          sum(1 for c in res3.cues if not c.dropped) == 2)


def parse_srt_text(text: str):
    from pathlib import Path
    import tempfile
    tmp = Path(tempfile.mkdtemp()) / "x.srt"
    # newline="" prevents Windows LF->CRLF translation of the CRLF sample.
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    return parse_srt(tmp)


# --------------------------------------------------------------------------
# Requirement 6: container format + index-only info + tamper resistance
# --------------------------------------------------------------------------

def test_container(base: Path) -> None:
    d = base / "t6_container"
    build_standard_suite(d)
    code, _out, err = _run_cli("package", "--manifest", "manifest.json",
                               "--root", ".", "--out", "master.mvst",
                               "--force", cwd=d)
    check("container package", code == 0, err)
    master = d / "master.mvst"
    blob = master.read_bytes()
    check("container magic", blob[0:4] == MAGIC)
    magic, version, sr, vf, ac, sc, tc, flags = HEADER_STRUCT.unpack_from(blob, 0)
    check("container version", version == 1)
    check("container counts", ac == 4 and sc == 3 and tc == 8)

    code, info_out, err = _run_cli("info", "master.mvst", cwd=d)
    check("info exit 0", code == 0, err)
    info = json.loads(info_out)
    file_size = len(blob)
    for track in info["tracks"]:
        check(f"index bounds {track['type']}{track['track_number']}",
              track["byte_offset"] + track["length"] <= file_size)

    # Independent structural check using only header bytes.
    header = read_master_header(master)
    for entry, track in zip(header.entries, info["tracks"]):
        check(f"info matches header {entry.track_type}/{entry.track_number}",
              entry.byte_offset == track["byte_offset"]
              and entry.data_length == track["length"]
              and entry.start_unit == track["start_unit"]
              and entry.end_unit == track["end_unit"])

    # Tamper an audio block's data bytes; info (header-only) must not change.
    before = json.loads(_run_cli("info", "master.mvst", cwd=d)[1])
    audio_entry = next(e for e in header.entries if e.track_type == TYPE_AUDIO)
    tampered = bytearray(blob)
    pos = audio_entry.byte_offset + BLOCK_HEADER_SIZE + 5
    tampered[pos] ^= 0xFF
    master.write_bytes(bytes(tampered))
    code, after_out, err = _run_cli("info", "master.mvst", cwd=d)
    after = json.loads(after_out)
    check("tamper info unchanged", before == after and code == 0, err)

    # Bad magic -> E_MASTER_INVALID.
    bad_magic = bytearray(blob)
    bad_magic[0:4] = b"XXXX"
    (d / "bad.mvst").write_bytes(bytes(bad_magic))
    code, _out, err = _run_cli("info", "bad.mvst", cwd=d)
    payload = _json_line(err)
    check("info bad magic", code == 2
          and payload["error_code"] == "E_MASTER_INVALID", err)

    code, _out, err = _run_cli("info", "missing.mvst", cwd=d)
    payload = _json_line(err)
    check("info missing file", code == 2
          and payload["error_code"] == "E_MASTER_INVALID", err)


# --------------------------------------------------------------------------
# Requirement 7: deterministic report + key order + shuffle env
# --------------------------------------------------------------------------

def test_report_determinism(base: Path) -> None:
    d = base / "t7_report"
    build_standard_suite(d)
    code, out1, err = _run_cli("validate", "--manifest", "manifest.json",
                               "--root", ".", cwd=d)
    check("report validate exit", code == 0, err)
    code, out2, _err = _run_cli("validate", "--manifest", "manifest.json",
                                "--root", ".", cwd=d)
    r1, r2 = json.loads(out1), json.loads(out2)
    r1.pop("generated_at"), r2.pop("generated_at")
    check("report identical rerun", r1 == r2)

    code, out3, err = _run_cli("validate", "--manifest", "manifest.json",
                               "--root", ".", cwd=d,
                               env={"MVST_SHUFFLE": "1"})
    check("report shuffle exit", code == 0, err)
    r3 = json.loads(out3)
    r3.pop("generated_at")
    check("report shuffle identical", r1 == r3)
    check("report key order",
          tuple(k for k in json.loads(out1).keys()) == REPORT_KEY_ORDER)
    check("report nested integer rms",
          isinstance(json.loads(out1)["audio"][0]["rms"], (int, float)))


# --------------------------------------------------------------------------
# Requirement 8: language consistency
# --------------------------------------------------------------------------

def test_languages(base: Path) -> None:
    d = base / "t8_lang"
    build_standard_suite(d)
    # Partial: French has audio but no subtitle.
    m = standard_manifest(subtitle_languages=("en", "es"))
    write_manifest(d / "manifest.json", m)
    code, out, err = _run_cli("validate", "--manifest", "manifest.json",
                              "--root", ".", cwd=d)
    report = json.loads(out)
    codes = [w["error_code"] for w in report["warnings"]]
    check("lang partial exit 0", code == 0, err)
    check("lang partial warning", "E_LANG_PARTIAL" in codes)

    # Both missing: declare a language with neither element.
    d2 = base / "t8_lang_missing"
    build_standard_suite(d2)
    m2 = load_manifest(d2 / "manifest.json").raw
    m2["languages"] = ["en", "es", "fr", "ja", "de"]
    write_manifest(d2 / "manifest.json", m2)
    code, _out, err = _run_cli("validate", "--manifest", "manifest.json",
                               "--root", ".", cwd=d2)
    payload = _json_line(err)
    check("lang missing exit 2", code == 2
          and payload["error_code"] == "E_LANG_MISSING"
          and payload.get("language") == "de", err)

    # Duplicate audio language.
    d3 = base / "t8_lang_dup"
    build_standard_suite(d3)
    m3 = load_manifest(d3 / "manifest.json").raw
    m3["tracks"][1]["language"] = "en"
    write_manifest(d3 / "manifest.json", m3)
    code, _out, err = _run_cli("validate", "--manifest", "manifest.json",
                               "--root", ".", cwd=d3)
    payload = _json_line(err)
    check("lang dup exit 2", code == 2
          and payload["error_code"] == "E_LANG_DUP", err)


# --------------------------------------------------------------------------
# Requirement 9: concurrency, per-thread buffers, collected failures
# --------------------------------------------------------------------------

def test_concurrency(base: Path) -> None:
    d = base / "t9_jobs"
    build_standard_suite(d)

    def run(jobs: int) -> tuple[int, dict, dict]:
        code, out, err = _run_cli(
            "validate", "--manifest", "manifest.json", "--root", ".",
            "--jobs", str(jobs), cwd=d, env={"MVST_FAIL_TRACK": "2"},
        )
        return code, json.loads(out) if out.strip() else {}, _json_line(err)

    code4, report4, err4 = run(4)
    code1, report1, err1 = run(1)
    check("jobs fail exit 2", code4 == 2 and err4["error_code"] == "E_RESAMPLE"
          and err4.get("track") == 2, str(err4))
    check("jobs fail jobs1 identical code",
          code1 == 2 and err1["error_code"] == "E_RESAMPLE"
          and err1.get("track") == 2)
    # Successful tracks 0, 1 and 3 still have statistics in both reports
    # while the failing track 2 is collected and reported via stderr.
    success4 = {a["index"]: a for a in report4["audio"]}
    success1 = {a["index"]: a for a in report1["audio"]}
    check("jobs partial stats", set(success4) == {0, 1, 3}
          and set(success1) == {0, 1, 3},
          f"{set(success4)} / {set(success1)}")
    check("jobs stats stable across parallelism",
          success4 == success1)


# --------------------------------------------------------------------------
# Requirement 10: atomic delivery, SIGINT cleanup, --force
# --------------------------------------------------------------------------

def test_atomic(base: Path) -> None:
    d = base / "t10_atomic"
    build_standard_suite(d)
    target = d / "master.mvst"
    target.write_bytes(b"pre-existing-content")
    digest_before = hashlib.sha256(target.read_bytes()).hexdigest()

    env = dict(os.environ)
    env["MVST_SLOW_WRITE"] = "1"
    creationflags = 0
    if os.name == "nt":
        # A new process group lets us deliver CTRL_BREAK (the Windows
        # equivalent of Ctrl+C) without interrupting the test runner itself.
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
    proc = subprocess.Popen(
        [_PY, "-m", "mvst", "package", "--manifest", "manifest.json",
         "--root", ".", "--out", "master.mvst", "--force"],
        cwd=str(d), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, creationflags=creationflags,
    )
    time.sleep(1.0)
    try:
        if os.name == "nt":
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            proc.send_signal(signal.SIGINT)
        out, err = proc.communicate(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate(timeout=20)
    code = proc.returncode
    tmp_left = list(d.glob("*.tmp"))
    digest_after = hashlib.sha256(target.read_bytes()).hexdigest()
    check("sigint exit 130", code == 130, f"code={code} err={err!r}")
    check("sigint temp removed", not tmp_left, str(tmp_left))
    check("sigint target intact", digest_before == digest_after)

    # Existing target without --force -> E_TARGET_EXISTS.
    code, _out, err = _run_cli("package", "--manifest", "manifest.json",
                               "--root", ".", "--out", "master.mvst", cwd=d)
    payload = _json_line(err)
    check("target exists guard", code == 2
          and payload["error_code"] == "E_TARGET_EXISTS", err)

    # --force overwrites.
    code, _out, err = _run_cli("package", "--manifest", "manifest.json",
                               "--root", ".", "--out", "master.mvst",
                               "--force", cwd=d)
    check("force overwrite", code == 0
          and hashlib.sha256(target.read_bytes()).hexdigest() != digest_before,
          err)


# --------------------------------------------------------------------------
# Requirement 11: saturation boundaries + independent point-wise extraction
# --------------------------------------------------------------------------

def test_saturation(base: Path) -> None:
    d = base / "t11_clip"
    d.mkdir()
    write_wav(d / "audio_en.wav", 48000, sine_samples(48000, 1.0, 1.0))
    (d / "video.raw").write_bytes(build_video(25))
    manifest = {
        "master_sample_rate": 48000, "video_fps": 25,
        "video_file": "video.raw", "languages": ["en"],
        "tracks": [{"language": "en", "offset_ms": 0, "gain_db": 12,
                    "file": "audio_en.wav"}],
        "subtitles": [],
    }
    write_manifest(d / "manifest.json", manifest)
    code, out, err = _run_cli("package", "--manifest", "manifest.json",
                              "--root", ".", "--out", "clip.mvst",
                              "--force", cwd=d)
    report = json.loads(out)
    check("saturation exit", code == 0, err)
    audio = report["audio"][0]
    codes = [w["error_code"] for w in report["warnings"]]
    check("clip risk warning", "E_CLIP_RISK" in codes)
    check("clip count positive", audio["clip_count"] > 0)
    check("peak full scale", audio["peak"] == 32767)
    check("rms six decimals", round(audio["rms"], 6) == audio["rms"])

    # Independent point-wise verification straight from the container index.
    header = read_master_header(d / "clip.mvst")
    entry = next(e for e in header.entries if e.track_type == TYPE_AUDIO)
    payload = extract_block(d / "clip.mvst", entry)
    values = struct.unpack("<%dh" % (len(payload) // 2), payload)
    in_range = all(-32768 <= v <= 32767 for v in values)
    sign_flip = any(values[i - 1] >= 32700 and values[i] <= -32700
                    for i in range(1, len(values)))
    check("samples in saturated range", in_range)
    check("no wraparound sign flip", not sign_flip)


# --------------------------------------------------------------------------
# Requirement 12: CLI contract
# --------------------------------------------------------------------------

def test_cli_contract(base: Path) -> None:
    d = base / "t12_cli"
    build_standard_suite(d)
    code, _out, err = _run_cli("validate", "--manifest", "manifest.json",
                               "--root", ".", cwd=d)
    check("cli validate 0", code == 0, err)

    bad = standard_manifest()
    bad["tracks"][0]["offset_ms"] = 6000
    write_manifest(d / "bad_manifest.json", bad)
    code, _out, err = _run_cli("validate", "--manifest", "bad_manifest.json",
                               "--root", ".", cwd=d)
    payload = _json_line(err)
    check("cli business 2", code == 2 and payload["error_code"].startswith("E_"))
    check("cli no traceback", "Traceback" not in err)

    code, _out, err = _run_cli("info", "nope.mvst", cwd=d)
    check("cli info 2", code == 2
          and _json_line(err)["error_code"] == "E_MASTER_INVALID")

    code, out, err = _run_cli("selftest-inner-placeholder", cwd=d)
    # The real selftest is the outer process; here assert parser rejection of
    # an unknown subcommand stays well-formed.
    check("cli unknown handled", code != 0 and "Traceback" not in err)


# --------------------------------------------------------------------------

def run_selftest() -> int:
    print("=" * 68)
    print("MVST deterministic self-test")
    print("=" * 68)
    base = Path(tempfile.mkdtemp(prefix="mvst_selftest_"))
    try:
        test_wav(base)
        test_manifest(base)
        test_timebase()
        test_timebase_report(base)
        test_resampler()
        test_package_determinism(base)
        test_subtitles()
        test_container(base)
        test_report_determinism(base)
        test_languages(base)
        test_concurrency(base)
        test_atomic(base)
        test_saturation(base)
        test_cli_contract(base)
    finally:
        shutil.rmtree(base, ignore_errors=True)

    passed = sum(1 for _, ok, _ in _results if ok)
    total = len(_results)
    print("-" * 68)
    print(f"{passed}/{total} checks passed")
    if passed != total:
        for name, ok, detail in _results:
            if not ok:
                print(f"FAILED: {name} {detail}")
        return 3
    print("ALL CHECKS PASSED")
    return 0
