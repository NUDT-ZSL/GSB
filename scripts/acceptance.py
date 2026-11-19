#!/usr/bin/env python3
"""Run the fixed local sample twice and compare byte-for-byte outputs."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CTL = ROOT / "configctl.py"
SNAPSHOT = ROOT / "samples" / "config_snapshot.json"
PATCH_2 = ROOT / "samples" / "patch_002.json"
PATCH_3 = ROOT / "samples" / "patch_003.json"
INPUT = ROOT / "samples" / "input_records.json"
TOLERANCE = ROOT / "samples" / "tolerance.json"


def _env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)
    if extra:
        env.update(extra)
    return env


def _run(
    arguments: list[str],
    *,
    expected_exit: int = 0,
    extra_env: dict[str, str] | None = None,
) -> str:
    completed = subprocess.run(
        [sys.executable, str(CTL), *arguments],
        cwd=str(ROOT),
        env=_env(extra_env),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != expected_exit:
        raise RuntimeError(
            f"command failed ({completed.returncode}, expected {expected_exit}): "
            f"{' '.join(arguments)}\nstdout={completed.stdout}\n"
            f"stderr={completed.stderr}"
        )
    return completed.stdout


def _common(db: Path) -> list[str]:
    return [
        "--db",
        str(db),
        "--snapshot",
        str(SNAPSHOT),
        "--tolerance-file",
        str(TOLERANCE),
    ]


def _run_deterministic_sequence(root: Path) -> dict[str, Any]:
    db = root / "hub.db"
    result_v1 = root / "result-v1.json"
    result_v2 = root / "result-v2.json"
    result_v3 = root / "result-v3.json"
    artifacts: list[str] = []
    commands: list[list[str]] = [
        ["init", *_common(db)],
        [
            "compute",
            *_common(db),
            "--client",
            "client-a",
            "--round",
            "1",
            "--now-ms",
            "1200",
            "--input",
            str(INPUT),
            "--output",
            str(result_v1),
        ],
        ["apply", *_common(db), str(PATCH_2), "--now-ms", "2000"],
        ["apply", *_common(db), str(PATCH_2), "--now-ms", "2100"],
        [
            "compute",
            *_common(db),
            "--client",
            "client-a",
            "--round",
            "2",
            "--now-ms",
            "2600",
            "--input",
            str(INPUT),
            "--output",
            str(result_v2),
        ],
        ["apply", *_common(db), str(PATCH_3), "--now-ms", "3000"],
        [
            "compute",
            *_common(db),
            "--client",
            "client-b",
            "--round",
            "1",
            "--now-ms",
            "3200",
            "--input",
            str(INPUT),
            "--output",
            str(result_v3),
        ],
        ["status", *_common(db), "--compact"],
        [
            "verify",
            *_common(db),
            "--replay-version",
            "1",
            "--input",
            str(INPUT),
            "--compact",
        ],
        ["resume", *_common(db), "--compact"],
    ]
    for command in commands:
        artifacts.append(_run(command))
    result_files = [
        result_v1.read_text(encoding="utf-8"),
        result_v2.read_text(encoding="utf-8"),
        result_v3.read_text(encoding="utf-8"),
    ]
    status = json.loads(artifacts[7])
    verify = json.loads(artifacts[8])
    return {
        "artifacts": artifacts,
        "result_files": result_files,
        "status": status,
        "verify": verify,
    }


def _crash_recovery_checks(root: Path) -> dict[str, bool]:
    patch_db = root / "patch-crash.db"
    _run(["init", *_common(patch_db)])
    _run(
        ["apply", *_common(patch_db), str(PATCH_2), "--now-ms", "2000"],
        expected_exit=90,
        extra_env={"CONFIG_HUB_FAILPOINT": "after_patch_prepare"},
    )
    before = json.loads(_run(["status", *_common(patch_db), "--compact"]))
    _run(["resume", *_common(patch_db)])
    after = json.loads(_run(["status", *_common(patch_db), "--compact"]))
    patch_verify = json.loads(_run(["verify", *_common(patch_db), "--compact"]))
    patch_ok = (
        before["patches"]["prepared"] == 1
        and before["current_version"] == 1
        and after["patches"]["prepared"] == 0
        and after["current_version"] == 2
        and patch_verify["ok"]
    )

    compute_db = root / "compute-crash.db"
    _run(["init", *_common(compute_db)])
    _run(
        [
            "compute",
            *_common(compute_db),
            "--client",
            "client-crash",
            "--round",
            "1",
            "--now-ms",
            "1200",
            "--input",
            str(INPUT),
        ],
        expected_exit=90,
        extra_env={"CONFIG_HUB_FAILPOINT": "before_result_commit"},
    )
    before_compute = json.loads(
        _run(["status", *_common(compute_db), "--compact"])
    )
    _run(["resume", *_common(compute_db)])
    after_compute = json.loads(_run(["status", *_common(compute_db), "--compact"]))
    compute_verify = json.loads(
        _run(["verify", *_common(compute_db), "--compact"])
    )
    compute_ok = (
        before_compute["compute_runs"]["in_progress"] == 1
        and after_compute["compute_runs"]["in_progress"] == 0
        and after_compute["compute_runs"]["completed"] == 1
        and compute_verify["ok"]
    )
    return {
        "prepared_patch": patch_ok,
        "incomplete_compute": compute_ok,
    }


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="config-hub-acceptance-") as temp:
        root = Path(temp)
        first = _run_deterministic_sequence(root / "run1")
        second = _run_deterministic_sequence(root / "run2")
        deterministic = (
            first["artifacts"] == second["artifacts"]
            and first["result_files"] == second["result_files"]
        )
        crash = _crash_recovery_checks(root / "crash")
        blob = json.dumps(
            {
                "artifacts": first["artifacts"],
                "result_files": first["result_files"],
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        summary = {
            "ok": deterministic
            and first["verify"]["ok"]
            and all(crash.values()),
            "deterministic_two_runs": deterministic,
            "sequence_sha256": "sha256:" + hashlib.sha256(blob).hexdigest(),
            "current_version": first["status"]["current_version"],
            "version_chain": [
                item["version"] for item in first["status"]["version_chain"]
            ],
            "clients": [
                {
                    "client_id": item["client_id"],
                    "bound_version": item["bound_version"],
                    "result_digest": item["result_digest"],
                }
                for item in first["status"]["clients"]
            ],
            "verify_ok": first["verify"]["ok"],
            "replay_v1_exact": first["verify"]["replay"]["entries"][0][
                "exact_match"
            ],
            "crash_recovery": crash,
        }
        print(json.dumps(summary, sort_keys=True, indent=2, ensure_ascii=False))
        return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
