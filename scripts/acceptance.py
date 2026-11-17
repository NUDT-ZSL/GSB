"""End-to-end local acceptance run.

The verifier consumes only generated description sets, their sync markers and
injection parameter objects, plus the engine report.  It does not inspect
generator-private intermediate state.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "artifacts" / "acceptance"


def _run(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *arguments],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def _hash_tree(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _projection(report: dict[str, Any]) -> list[dict[str, Any]]:
    keys = (
        "dataset_id",
        "status",
        "sync_marker_ok",
        "codes",
        "rejected_records",
        "duplicate_edges",
        "cycles",
        "violations",
        "fields",
        "observations",
    )
    return [{key: dataset[key] for key in keys} for dataset in report["datasets"]]


def main() -> int:
    if OUTPUT.exists():
        shutil.rmtree(OUTPUT)
    OUTPUT.mkdir(parents=True)
    samples = OUTPUT / "samples"
    reports = OUTPUT / "reports"
    reports.mkdir()

    generated = _run(
        "-m",
        "sensitive_guard.generator",
        "--out-dir",
        str(samples),
        "--seed",
        "20261118",
        "--clean",
    )
    if generated.returncode != 0:
        print(generated.stderr, file=sys.stderr)
        return 1
    input_hashes_before = _hash_tree(samples)

    clean = _run(
        "-m",
        "sensitive_guard",
        "validate",
        "--input",
        str(samples / "batch.json"),
        "--dataset-id",
        "clean",
        "--report",
        str(reports / "clean.json"),
        "--concurrency",
        "2",
    )

    serial = _run(
        "-m",
        "sensitive_guard",
        "validate",
        "--input",
        str(samples / "batch.json"),
        "--report",
        str(reports / "serial.json"),
        "--concurrency",
        "1",
        "--timeout-ms",
        "100",
        "--max-fields",
        "30",
    )
    concurrent = _run(
        "-m",
        "sensitive_guard",
        "validate",
        "--input",
        str(samples / "batch.json"),
        "--report",
        str(reports / "concurrent.json"),
        "--concurrency",
        "4",
        "--timeout-ms",
        "100",
        "--max-fields",
        "30",
    )
    parameter_error = _run(
        "-m",
        "sensitive_guard",
        "validate",
        "--input",
        str(samples / "batch.json"),
        "--report",
        str(reports / "invalid.json"),
        "--concurrency",
        "0",
    )

    failures: list[str] = []
    if clean.returncode != 0:
        failures.append(f"clean exit was {clean.returncode}")
    if serial.returncode != 1 or concurrent.returncode != 1:
        failures.append(
            f"business-failure exits were {serial.returncode}/{concurrent.returncode}"
        )
    if parameter_error.returncode != 2:
        failures.append(f"parameter exit was {parameter_error.returncode}")

    report_paths = [reports / "clean.json", reports / "serial.json", reports / "concurrent.json"]
    parsed: list[dict[str, Any]] = []
    for report_path in report_paths:
        try:
            parsed.append(json.loads(report_path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as exc:
            failures.append(f"cannot parse {report_path.name}: {exc}")
    if len(parsed) != 3:
        print(json.dumps({"failures": failures}, ensure_ascii=False, indent=2))
        return 1

    clean_report, serial_report, concurrent_report = parsed
    if clean_report["summary"]["all_success"] is not True:
        failures.append("clean subset did not succeed")
    if serial_report["summary"]["all_success"] is not False:
        failures.append("mixed batch did not report failure")
    if _projection(serial_report) != _projection(concurrent_report):
        failures.append("concurrency changed dataset semantics or ordering")
    if serial_report["summary"]["observation_inconsistent"] != 0:
        failures.append("serial observations were inconsistent")
    if concurrent_report["summary"]["observation_inconsistent"] != 0:
        failures.append("concurrent observations were inconsistent")

    expected_codes = {
        "E_TIMEOUT",
        "E_FIELDS",
        "E_CYCLE",
        "E_VERSION_CONFLICT",
        "E_DOWNGRADE",
        "E_LEAK",
    }
    observed_codes = set(serial_report["summary"]["code_counts"])
    if not expected_codes.issubset(observed_codes):
        failures.append(
            "missing expected codes: "
            + ", ".join(sorted(expected_codes - observed_codes))
        )

    cycle_dataset = next(
        dataset
        for dataset in serial_report["datasets"]
        if dataset["dataset_id"] == "cycle_injection"
    )
    if not cycle_dataset["cycles"] or cycle_dataset["cycles"][0]["length"] != 4:
        failures.append("forced four-node cycle was not reproduced")

    input_hashes_after = _hash_tree(samples)
    if input_hashes_before != input_hashes_after:
        failures.append("generated input files changed during validation")
    if list(reports.glob("*.tmp")):
        failures.append("temporary report files were left behind")

    result = {
        "exit_codes": {
            "clean": clean.returncode,
            "mixed": serial.returncode,
            "concurrent": concurrent.returncode,
            "parameter_error": parameter_error.returncode,
        },
        "dataset_count": serial_report["summary"]["dataset_count"],
        "code_counts": serial_report["summary"]["code_counts"],
        "observations": {
            "consistent": serial_report["summary"]["observation_consistent"],
            "inconsistent": serial_report["summary"]["observation_inconsistent"],
        },
        "force_cycle_path": (
            cycle_dataset["cycles"][0]["path"] if cycle_dataset["cycles"] else None
        ),
        "concurrency_semantics_equal": _projection(serial_report)
        == _projection(concurrent_report),
        "input_unchanged": input_hashes_before == input_hashes_after,
        "reports_parseable": True,
        "failures": failures,
    }
    (OUTPUT / "acceptance-summary.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
