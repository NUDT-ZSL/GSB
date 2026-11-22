#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from h2diag.canonical import digest_json  # noqa: E402
from h2diag.samples import generate_samples  # noqa: E402
from h2diag.service import DiagnosticService  # noqa: E402


def _env(**values: str) -> dict[str, str]:
    result = os.environ.copy()
    result["PYTHONPATH"] = str(ROOT)
    result.update(values)
    return result


def _cli(
    args: list[str],
    *,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "h2diag", *args],
        cwd=ROOT,
        env=_env(**(extra_env or {})),
        text=True,
        capture_output=True,
        timeout=120,
    )


def _baseline_patch_digest(work: Path, samples: Path) -> tuple[str, dict[str, Any]]:
    service = DiagnosticService(work / "baseline-patch.db")
    service.initialize_from_file(samples / "scenario.json")
    service.run("demo-connection", max_frames=12)
    service.apply_patch(
        "demo-connection",
        json.loads(
            (samples / "patches" / "settings-weight-v2.json").read_text(encoding="utf-8")
        ),
    )
    service.resume("demo-connection")
    status = service.status("demo-connection")
    verify = service.verify("demo-connection")
    return status["state_digest"], {"status": status, "verify": verify}


def main() -> int:
    samples = ROOT / "samples"
    manifest = generate_samples(samples)
    report_path = (
        Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "artifacts" / "acceptance-report.json"
    )
    with tempfile.TemporaryDirectory(prefix="h2diag-acceptance-") as temporary:
        work = Path(temporary)
        baseline_service = DiagnosticService(work / "baseline-frame.db")
        baseline_service.initialize_from_file(samples / "scenario.json")
        baseline_service.run("demo-connection")
        baseline_digest = baseline_service.status("demo-connection")["state_digest"]
        baseline_verify = baseline_service.verify("demo-connection")

        frame_db = work / "crash-frame.db"
        frame_init = _cli(
            ["init", "--db", str(frame_db), "--scenario", str(samples / "scenario.json")]
        )
        frame_crash = _cli(
            ["run", "--db", str(frame_db), "--connection-id", "demo-connection"],
            extra_env={
                "H2DIAG_CRASH_POINT": "frame_mid_tx",
                "H2DIAG_CRASH_SEQ": "5",
            },
        )
        frame_service = DiagnosticService(frame_db)
        frame_after_crash = frame_service.status("demo-connection")
        frame_resume = frame_service.resume("demo-connection")
        frame_verify = frame_service.verify("demo-connection")

        patch_digest, patch_baseline = _baseline_patch_digest(work, samples)
        patch_db = work / "crash-patch.db"
        patch_init = _cli(
            ["init", "--db", str(patch_db), "--scenario", str(samples / "scenario.json")]
        )
        patch_prefix = _cli(
            [
                "run",
                "--db",
                str(patch_db),
                "--connection-id",
                "demo-connection",
                "--max-frames",
                "12",
            ]
        )
        patch_crash = _cli(
            [
                "patch",
                "--db",
                str(patch_db),
                "--patch",
                str(samples / "patches" / "settings-weight-v2.json"),
            ],
            extra_env={"H2DIAG_CRASH_POINT": "patch_mid_tx"},
        )
        patch_service = DiagnosticService(patch_db)
        patch_after_crash = patch_service.status("demo-connection")
        patch_retry = patch_service.apply_patch(
            "demo-connection",
            json.loads(
                (samples / "patches" / "settings-weight-v2.json").read_text(encoding="utf-8")
            ),
        )
        patch_service.resume("demo-connection")
        patch_verify = patch_service.verify("demo-connection")

        concurrent_db = work / "concurrent.db"
        processes = [
            subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "h2diag",
                    "run",
                    "--db",
                    str(concurrent_db),
                    "--scenario",
                    str(samples / "scenario.json"),
                ],
                cwd=ROOT,
                env=_env(),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(4)
        ]
        process_results = []
        for process in processes:
            _, stderr = process.communicate(timeout=120)
            process_results.append(
                {"returncode": process.returncode, "stderr": stderr.strip()}
            )
        concurrent_service = DiagnosticService(concurrent_db)
        concurrent_status = concurrent_service.status("demo-connection")
        concurrent_verify = concurrent_service.verify("demo-connection")
        with sqlite3.connect(concurrent_db) as connection:
            frame_count = connection.execute(
                "SELECT COUNT(*), COUNT(DISTINCT seq), MIN(seq), MAX(seq) FROM frames"
            ).fetchone()
            commit_count = connection.execute(
                "SELECT COUNT(*) FROM state_commits WHERE kind='frame'"
            ).fetchone()[0]

        pytest_result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q"],
            cwd=ROOT,
            env=_env(),
            text=True,
            capture_output=True,
            timeout=120,
        )
        pytest_summary = ""
        if pytest_result.stdout.strip():
            pytest_summary = pytest_result.stdout.strip().splitlines()[-1].split(" in ")[0]

        report: dict[str, Any] = {
            "ok": all(
                [
                    baseline_verify["ok"],
                    frame_init.returncode == 0,
                    frame_crash.returncode == 86,
                    frame_after_crash["committed_seq"] == 4,
                    frame_resume["processed"] == 14,
                    frame_resume["abandoned_intents"] == 1,
                    frame_service.status("demo-connection")["state_digest"] == baseline_digest,
                    frame_verify["ok"],
                    patch_init.returncode == 0,
                    patch_prefix.returncode == 0,
                    patch_crash.returncode == 86,
                    patch_after_crash["patch_version"] == 1,
                    patch_retry["version"] == 2,
                    patch_service.status("demo-connection")["state_digest"] == patch_digest,
                    patch_verify["ok"],
                    all(item["returncode"] == 0 for item in process_results),
                    tuple(frame_count) == (18, 18, 1, 18),
                    commit_count == 18,
                    concurrent_status["committed_seq"] == 18,
                    concurrent_verify["ok"],
                    pytest_result.returncode == 0,
                ]
            ),
            "samples_manifest_digest": manifest["manifest_digest"],
            "pytest": {
                "returncode": pytest_result.returncode,
                "summary": pytest_summary,
            },
            "baseline": {
                "state_digest": baseline_digest,
                "verify_ok": baseline_verify["ok"],
            },
            "frame_crash": {
                "exit_code": frame_crash.returncode,
                "watermark_after_crash": frame_after_crash["committed_seq"],
                "resumed_frames": frame_resume["processed"],
                "abandoned_intents": frame_resume["abandoned_intents"],
                "state_digest": frame_service.status("demo-connection")["state_digest"],
                "verify_ok": frame_verify["ok"],
            },
            "patch_crash": {
                "exit_code": patch_crash.returncode,
                "version_after_crash": patch_after_crash["patch_version"],
                "retried_version": patch_retry["version"],
                "state_digest": patch_service.status("demo-connection")["state_digest"],
                "verify_ok": patch_verify["ok"],
            },
            "multiprocess": {
                "process_results": process_results,
                "frame_rows": frame_count,
                "frame_state_commits": commit_count,
                "watermark": concurrent_status["committed_seq"],
                "verify_ok": concurrent_verify["ok"],
            },
        }
    report["report_digest"] = digest_json(report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
