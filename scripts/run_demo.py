from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import generate_samples  # noqa: E402


def _run_cli(*args: str) -> tuple[int, dict[str, Any]]:
    process = subprocess.run(
        [sys.executable, "-m", "audit_forge", *args],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    output = process.stdout.strip()
    return (
        process.returncode,
        json.loads(output) if output else {},
    )


def _case_identity(report: dict[str, Any]) -> list[tuple[str, tuple[str, ...]]]:
    return sorted(
        (case["case_id"], tuple(case["event_ids"]))
        for case in report.get("case_summaries", [])
    )


def _fresh_workspace(path: Path) -> Path:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)
    return path


def _internal_crash_import(workspace: Path, input_path: Path) -> int:
    from audit_forge import store
    from audit_forge.cli import main as cli_main

    original_replace = store.replace_file

    def crash_before_checkpoint(source: str | Path, destination: str | Path) -> None:
        if Path(destination).name == "checkpoint.json":
            os._exit(137)
        original_replace(source, destination)

    store.replace_file = crash_before_checkpoint
    return cli_main(
        [
            "import",
            "--workspace",
            str(workspace),
            "--input",
            str(input_path),
        ]
    )


def run_demo(output_dir: Path) -> dict[str, Any]:
    sample_dir = output_dir / "samples"
    sample_manifest = generate_samples.generate(sample_dir)
    work_dir = output_dir / "workspaces"
    work_dir.mkdir(parents=True, exist_ok=True)

    case_identities: dict[str, Any] = {}
    for kind in ("ordered", "shuffled", "duplicates"):
        workspace = _fresh_workspace(work_dir / f"ws-{kind}")
        code, report = _run_cli(
            "import",
            "--workspace",
            str(workspace),
            "--input",
            str(sample_dir / f"events_{kind}.jsonl"),
            "--checkpoint-every",
            "1",
        )
        case_identities[kind] = {
            "exit_code": code,
            "status": report.get("status"),
            "cases": _case_identity(report),
            "anomaly_count": report.get("counts", {}).get("anomalies"),
            "generation": report.get("generation_after"),
        }

    shuffled_matches = (
        case_identities["ordered"]["cases"]
        == case_identities["shuffled"]["cases"]
    )
    duplicates_match = (
        case_identities["ordered"]["cases"]
        == case_identities["duplicates"]["cases"]
    )

    anomaly_code, anomaly_report = _run_cli(
        "import",
        "--workspace",
        str(_fresh_workspace(work_dir / "ws-anomalies")),
        "--input",
        str(sample_dir / "events_anomalies.jsonl"),
    )
    anomaly_codes = sorted(
        {
            item["code"]
            for item in anomaly_report.get("anomalies", [])
            if item["code"]
            in {
                "E_ORPHAN",
                "E_CYCLE",
                "E_CROSS_TRACE",
                "E_SEQ_GAP",
                "E_EVENT_CONFLICT",
            }
        }
    )

    version_workspace = _fresh_workspace(work_dir / "ws-version")
    before_code, before_report = _run_cli(
        "import",
        "--workspace",
        str(version_workspace),
        "--input",
        str(sample_dir / "events_ordered.jsonl"),
    )
    repair_code, repair_report = _run_cli(
        "check-checkpoint",
        "--workspace",
        str(version_workspace),
        "--rules",
        str(sample_dir / "rules-short-window.json"),
        "--repair",
    )

    crash_workspace = _fresh_workspace(work_dir / "ws-crash")
    events = generate_samples.clean_events()
    first_path = output_dir / "crash-first.jsonl"
    second_path = output_dir / "crash-second.jsonl"
    generate_samples.write_jsonl(first_path, events[:5])
    generate_samples.write_jsonl(second_path, events[5:])

    first_code, first_report = _run_cli(
        "import",
        "--workspace",
        str(crash_workspace),
        "--input",
        str(first_path),
    )
    crash_process = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "--internal-crash-import",
            str(crash_workspace),
            str(second_path),
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    check_code, check_report = _run_cli(
        "check-checkpoint",
        "--workspace",
        str(crash_workspace),
    )
    repair_crash_code, repair_crash_report = _run_cli(
        "check-checkpoint",
        "--workspace",
        str(crash_workspace),
        "--repair",
    )
    resume_code, resume_report = _run_cli(
        "import",
        "--workspace",
        str(crash_workspace),
        "--input",
        str(second_path),
    )

    summary = {
        "sample_manifest": sample_manifest,
        "case_consistency": {
            "identities": case_identities,
            "shuffled_matches_ordered": shuffled_matches,
            "duplicates_match_ordered": duplicates_match,
        },
        "anomaly_import": {
            "exit_code": anomaly_code,
            "status": anomaly_report.get("status"),
            "codes": anomaly_codes,
            "reject_codes": sorted(
                {item["code"] for item in anomaly_report.get("rejects", [])}
            ),
        },
        "rule_version_rebuild": {
            "before_exit_code": before_code,
            "before_cases": before_report.get("counts", {}).get("cases"),
            "repair_exit_code": repair_code,
            "repair_generation": repair_report.get("repair", {}).get(
                "generation"
            ),
            "after_cases": repair_report.get("counts", {}).get("cases"),
        },
        "forced_termination": {
            "first_import_exit_code": first_code,
            "first_generation": first_report.get("generation_after"),
            "crash_exit_code": crash_process.returncode,
            "recovered_generation": check_report.get("load", {}).get(
                "generation_before"
            ),
            "recovered_cases_source": check_report.get("load", {}).get(
                "cases_source"
            ),
            "check_exit_code": check_code,
            "repair_exit_code": repair_crash_code,
            "resume_exit_code": resume_code,
            "final_unique_events": resume_report.get("counts", {}).get(
                "unique_events"
            ),
        },
    }
    summary["ok"] = all(
        [
            case_identities["ordered"]["exit_code"] == 0,
            case_identities["shuffled"]["exit_code"] == 0,
            case_identities["duplicates"]["exit_code"] == 0,
            shuffled_matches,
            duplicates_match,
            anomaly_code == 1,
            {
                "E_ORPHAN",
                "E_CYCLE",
                "E_CROSS_TRACE",
                "E_SEQ_GAP",
                "E_EVENT_CONFLICT",
            }
            <= set(anomaly_codes),
            before_code == 0,
            repair_code == 0,
            repair_report.get("counts", {}).get("cases") == 0,
            crash_process.returncode == 137,
            check_code == 1,
            repair_crash_code == 0,
            resume_code == 0,
            resume_report.get("counts", {}).get("unique_events") == len(events),
        ]
    )
    summary_path = output_dir / "demo-summary.json"
    generate_samples.write_json(summary_path, summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output-dir",
        default="demo-run",
        help="directory for samples, workspaces, and summary",
    )
    parser.add_argument(
        "--internal-crash-import",
        nargs=2,
        metavar=("WORKSPACE", "INPUT"),
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args()
    if args.internal_crash_import:
        workspace, input_path = args.internal_crash_import
        return _internal_crash_import(Path(workspace), Path(input_path))
    summary = run_demo(Path(args.output_dir))
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
