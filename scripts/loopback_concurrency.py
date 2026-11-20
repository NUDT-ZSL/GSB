#!/usr/bin/env python3
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "sample"
CLOCK = 1_735_689_600_000
WORK = ROOT / "artifacts" / "loopback"


def run(command: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        command, check=False, cwd=ROOT, text=True, capture_output=True
    )


def main() -> int:
    if WORK.exists():
        shutil.rmtree(WORK)
    WORK.mkdir(parents=True)
    db = WORK / "state.sqlite3"
    load = run(
        [
            sys.executable,
            "-m",
            "tls_trust_pipeline",
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
        ]
    )
    if load.returncode != 0:
        raise RuntimeError(load.stderr)
    base = [sys.executable, "-m", "tls_trust_pipeline", "--db", str(db)]
    verify_command = [
        *base,
        "verify",
        "--requests",
        str(SAMPLE / "requests/handshake-requests.json"),
        "--cert-dir",
        str(SAMPLE / "certs"),
        "--revocation-evidence",
        str(SAMPLE / "revocation/evidence.json"),
    ]
    apply_command = [
        *base,
        "apply",
        "--patch",
        str(SAMPLE / "patches/p1-tombstone-root-retired.json"),
        "--cert-dir",
        str(SAMPLE / "certs"),
        "--logical-clock-ms",
        str(CLOCK),
    ]
    specs = [
        (subprocess.Popen(verify_command, cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE), "verify")
        for _ in range(4)
    ]
    specs.append(
        (
            subprocess.Popen(
                apply_command,
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            ),
            "apply",
        )
    )
    processes = []
    for process, kind in specs:
        stdout, stderr = process.communicate(timeout=120)
        processes.append(
            {
                "kind": kind,
                "returncode": process.returncode,
                "ok": process.returncode == 0 and json.loads(stdout).get("ok") is True,
                "stderr": stderr,
            }
        )
    final_verify = run(verify_command)
    status = run([*base, "status"])
    audit = run([*base, "verify-audit"])
    report = {
        "ok": all(item["ok"] for item in processes)
        and final_verify.returncode == 0
        and status.returncode == 0
        and audit.returncode == 0,
        "processes": processes,
        "status": json.loads(status.stdout) if status.returncode == 0 else None,
        "audit": json.loads(audit.stdout) if audit.returncode == 0 else None,
    }
    (WORK / "report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

