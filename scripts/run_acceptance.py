#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "sample"
CLOCK = 1_735_689_600_000
ARTIFACTS = ROOT / "artifacts" / "acceptance"


def run(command: list[str]) -> bytes:
    completed = subprocess.run(command, check=False, cwd=ROOT, capture_output=True)
    if completed.returncode != 0:
        raise RuntimeError(
            f"command failed: {' '.join(command)}\n"
            f"stdout={completed.stdout.decode(errors='replace')}\n"
            f"stderr={completed.stderr.decode(errors='replace')}"
        )
    return completed.stdout


def one_run(label: str) -> dict[str, bytes]:
    run_dir = ARTIFACTS / label
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True)
    db = run_dir / "state.sqlite3"
    base = [sys.executable, "-m", "tls_trust_pipeline", "--db", str(db)]
    commands = {
        "load.json": [
            *base,
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
        "chain.json": [
            *base,
            "chain",
            "--requests",
            str(SAMPLE / "requests/handshake-requests.json"),
            "--cert-dir",
            str(SAMPLE / "certs"),
            "--revocation-evidence",
            str(SAMPLE / "revocation/evidence.json"),
        ],
        "verify-before-patch.json": [
            *base,
            "verify",
            "--requests",
            str(SAMPLE / "requests/handshake-requests.json"),
            "--cert-dir",
            str(SAMPLE / "certs"),
            "--revocation-evidence",
            str(SAMPLE / "revocation/evidence.json"),
        ],
        "apply.json": [
            *base,
            "apply",
            "--patch",
            str(SAMPLE / "patches/p1-tombstone-root-retired.json"),
            "--cert-dir",
            str(SAMPLE / "certs"),
            "--logical-clock-ms",
            str(CLOCK),
        ],
        "verify-after-patch.json": [
            *base,
            "verify",
            "--requests",
            str(SAMPLE / "requests/handshake-requests.json"),
            "--cert-dir",
            str(SAMPLE / "certs"),
            "--revocation-evidence",
            str(SAMPLE / "revocation/evidence.json"),
        ],
        "status.json": [*base, "status"],
        "audit.json": [*base, "verify-audit"],
        "resume.json": [
            *base,
            "resume",
            "--cert-dir",
            str(SAMPLE / "certs"),
            "--revocation-evidence",
            str(SAMPLE / "revocation/evidence.json"),
        ],
    }
    outputs = {}
    for filename, command in commands.items():
        data = run(command)
        parsed = json.loads(data)
        if parsed.get("ok") is not True:
            raise RuntimeError(f"{label}/{filename} did not report success")
        outputs[filename] = data
        (run_dir / filename).write_bytes(data)
    return outputs


def main() -> int:
    outputs_a = one_run("run-a")
    outputs_b = one_run("run-b")
    if set(outputs_a) != set(outputs_b):
        raise RuntimeError("run output files differ")
    per_file = {}
    for name in sorted(outputs_a):
        same = outputs_a[name] == outputs_b[name]
        per_file[name] = {
            "sha256": hashlib.sha256(outputs_a[name]).hexdigest(),
            "byte_identical": same,
        }
    report = {
        "ok": all(item["byte_identical"] for item in per_file.values()),
        "runs": 2,
        "state_databases": [
            "artifacts/acceptance/run-a/state.sqlite3",
            "artifacts/acceptance/run-b/state.sqlite3",
        ],
        "outputs": per_file,
        "note": "Each run uses a fresh database and the same injected clock.",
    }
    (ARTIFACTS / "report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

