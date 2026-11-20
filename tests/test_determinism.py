from __future__ import annotations

import json
from pathlib import Path

from conftest import CLOCK, SAMPLE, run_cli


def _run_pipeline(db: Path, work: Path) -> dict[str, bytes]:
    commands = [
        (
            "load",
            [
                "--db",
                str(db),
                "load",
                "--v1",
                str(SAMPLE / "trust/v1/trust-v1.json"),
                "--v2",
                str(SAMPLE / "trust/v2/manifest.json"),
                "--cert-dir",
                str(SAMPLE / "certs"),
                "--logical-clock-ms",
                str(CLOCK),
            ],
        ),
        (
            "verify",
            [
                "--db",
                str(db),
                "verify",
                "--requests",
                str(SAMPLE / "requests/handshake-requests.json"),
                "--cert-dir",
                str(SAMPLE / "certs"),
                "--revocation-evidence",
                str(SAMPLE / "revocation/evidence.json"),
            ],
        ),
        (
            "apply",
            [
                "--db",
                str(db),
                "apply",
                "--patch",
                str(SAMPLE / "patches/p1-tombstone-root-retired.json"),
                "--cert-dir",
                str(SAMPLE / "certs"),
                "--logical-clock-ms",
                str(CLOCK),
            ],
        ),
        (
            "verify_after_patch",
            [
                "--db",
                str(db),
                "verify",
                "--requests",
                str(SAMPLE / "requests/handshake-requests.json"),
                "--cert-dir",
                str(SAMPLE / "certs"),
                "--revocation-evidence",
                str(SAMPLE / "revocation/evidence.json"),
            ],
        ),
        ("status", ["--db", str(db), "status"]),
        ("audit", ["--db", str(db), "verify-audit"]),
        ("resume", ["--db", str(db), "resume", "--cert-dir", str(SAMPLE / "certs")]),
    ]
    outputs = {}
    for name, arguments in commands:
        completed = run_cli(*arguments)
        outputs[name] = completed.stdout.encode("utf-8")
        parsed = json.loads(completed.stdout)
        assert parsed["ok"] is True
    return outputs


def test_two_independent_runs_are_byte_identical(tmp_path: Path) -> None:
    first = _run_pipeline(tmp_path / "first.sqlite3", tmp_path / "first")
    second = _run_pipeline(tmp_path / "second.sqlite3", tmp_path / "second")
    assert first == second

