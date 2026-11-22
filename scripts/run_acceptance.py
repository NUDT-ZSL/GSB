#!/usr/bin/env python3
"""Run the deterministic sample through baseline and both crash boundaries."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "sample" / "bundle.json"
MANIFEST = ROOT / "sample" / "manifest.json"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "dynabatch", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def _require_ok(result: subprocess.CompletedProcess[str], label: str) -> None:
    if result.returncode != 0:
        raise RuntimeError(
            f"{label} failed with {result.returncode}: {result.stderr or result.stdout}"
        )


def _save(db: Path) -> None:
    _require_ok(
        _run("save", "--bundle", str(SAMPLE), "--db", str(db)),
        "save",
    )


def _schedule(
    db: Path, *, crash_at: str | None = None
) -> subprocess.CompletedProcess[str]:
    args = ["schedule", "--db", str(db), "--now", "300"]
    if crash_at:
        args.extend(["--crash-at", crash_at])
    return _run(*args)


def _resume(db: Path) -> None:
    _require_ok(_run("resume", "--db", str(db)), "resume")


def _train(db: Path) -> None:
    _require_ok(_run("train", "--db", str(db), "--now", "300"), "train")


def _status(db: Path) -> tuple[bytes, dict[str, Any]]:
    result = _run("status", "--db", str(db), "--canonical")
    _require_ok(result, "status")
    payload = result.stdout.encode("utf-8")
    return payload, json.loads(payload)


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def main() -> int:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if hashlib.sha256(SAMPLE.read_bytes()).hexdigest() != manifest["file_sha256"]:
        raise RuntimeError("sample file digest does not match manifest")
    report: dict[str, Any] = {
        "sample": {
            "path": "sample/bundle.json",
            "file_sha256": manifest["file_sha256"],
            "canonical_sha256": manifest["canonical_sha256"],
            "request_count": manifest["request_count"],
        },
        "logical_clock_ms": 300,
        "paths": {},
    }
    with tempfile.TemporaryDirectory(prefix="dynabatch-acceptance-") as temp:
        root = Path(temp)
        baseline = root / "baseline.db"
        _save(baseline)
        scheduled = _schedule(baseline)
        _require_ok(scheduled, "baseline schedule")
        _train(baseline)
        baseline_bytes, baseline_status = _status(baseline)
        report["baseline"] = {
            "status_sha256": _digest(baseline_bytes),
            "state_digest": baseline_status["state_digest"],
            "stats": baseline_status["stats"],
        }
        verify = _run("verify", "--db", str(baseline))
        _require_ok(verify, "verify")
        report["verify"] = json.loads(verify.stdout)

        for phase, expected_exit in (
            ("batch_decision", 86),
            ("window_commit", 87),
        ):
            db = root / f"{phase}.db"
            _save(db)
            crashed = _schedule(db, crash_at=phase)
            if crashed.returncode != expected_exit:
                raise RuntimeError(
                    f"{phase} expected exit {expected_exit}, got {crashed.returncode}"
                )
            _resume(db)
            _train(db)
            crashed_bytes, crashed_status = _status(db)
            baseline_compare = _run(
                "verify",
                "--db",
                str(db),
                "--baseline",
                str(baseline),
            )
            _require_ok(baseline_compare, f"{phase} baseline verify")
            report["paths"][phase] = {
                "injected_exit_code": crashed.returncode,
                "resume_exit_code": 0,
                "status_sha256": _digest(crashed_bytes),
                "state_digest": crashed_status["state_digest"],
                "byte_identical_to_baseline": crashed_bytes == baseline_bytes,
                "digest_equal_to_baseline": (
                    crashed_status["state_digest"] == baseline_status["state_digest"]
                ),
                "verify": json.loads(baseline_compare.stdout),
            }
    report["result"] = (
        "pass"
        if all(
            item["byte_identical_to_baseline"]
            and item["digest_equal_to_baseline"]
            and item["verify"]["ok"]
            for item in report["paths"].values()
        )
        else "fail"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["result"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
