from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from tlsdiag.generate import generate_sample, verify_sample
from tlsdiag.locks import atomic_write_bytes, file_lock


ROOT = Path(__file__).resolve().parents[1]


def test_generation_is_byte_deterministic(tmp_path) -> None:
    first = generate_sample(tmp_path / "first", seed=7)
    second = generate_sample(tmp_path / "second", seed=7)
    assert first["sync_marker"] == second["sync_marker"]
    sync_a = json.loads((tmp_path / "first/_SYNC.json").read_text(encoding="utf-8"))
    sync_b = json.loads((tmp_path / "second/_SYNC.json").read_text(encoding="utf-8"))
    assert sync_a["files"] == sync_b["files"]
    expected = json.loads(
        (tmp_path / "first/expected/expected.json").read_text(encoding="utf-8")
    )
    assert expected["truth_digest"].startswith("sha256:")
    assert verify_sample(tmp_path / "first")["ok"]


def test_sync_marker_detects_tampering(tmp_path) -> None:
    out = tmp_path / "sample"
    generate_sample(out, seed=11)
    target = out / "input/handshake.jsonl"
    target.write_bytes(target.read_bytes() + b"\n")
    result = verify_sample(out)
    assert result["ok"] is False
    assert "input/handshake.jsonl" in result["mismatches"]


def test_cli_generate_verify_analyze(tmp_path) -> None:
    sample = tmp_path / "sample"
    generated = subprocess.run(
        [sys.executable, "-m", "tlsdiag", "generate", "--out", str(sample), "--seed", "13"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert generated.returncode == 0, generated.stderr + generated.stdout
    verified = subprocess.run(
        [sys.executable, "-m", "tlsdiag", "verify-sample", "--dir", str(sample), "--json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert verified.returncode == 0, verified.stderr + verified.stdout
    output = tmp_path / "out"
    analyzed = subprocess.run(
        [
            sys.executable,
            "-m",
            "tlsdiag",
            "analyze",
            "--handshake",
            str(sample / "input/handshake.jsonl"),
            "--v1",
            str(sample / "input/trust_v1.json"),
            "--v2-shards",
            str(sample / "input/trust_v2"),
            "--v2-db",
            str(sample / "input/trust_v2.sqlite3"),
            "--out",
            str(output),
            "--jobs",
            "3",
            "--json",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert analyzed.returncode == 0, analyzed.stderr + analyzed.stdout
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    assert report["summary"]["connections_total"] == 15


def test_cli_lock_conflict_exit_code(tmp_path, sample_dir) -> None:
    output = tmp_path / "locked"
    output.mkdir()
    with file_lock(output / ".tlsdiag.lock"):
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "tlsdiag",
                "analyze",
                "--handshake",
                str(sample_dir / "input/handshake.jsonl"),
                "--v1",
                str(sample_dir / "input/trust_v1.json"),
                "--v2-shards",
                str(sample_dir / "input/trust_v2"),
                "--v2-db",
                str(sample_dir / "input/trust_v2.sqlite3"),
                "--out",
                str(output),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
    assert result.returncode == 3
    payload = json.loads(result.stdout)
    assert payload["codes"] == ["E_LOCK"]


def test_atomic_write_replaces_whole_file(tmp_path) -> None:
    target = tmp_path / "report.json"
    atomic_write_bytes(target, b'{"old":true}\n')
    atomic_write_bytes(target, b'{"new":true}\n')
    assert target.read_bytes() == b'{"new":true}\n'
    assert not list(tmp_path.glob(".*.tmp"))
