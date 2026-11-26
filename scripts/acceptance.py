from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from capacity_engine.seed import generate_seed_directory  # noqa: E402
from capacity_engine.verify import verify_output_directory  # noqa: E402


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "capacity_engine.cli", *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )


def report_hashes(out: Path) -> dict[str, str]:
    return {
        path.name: __import__("hashlib").sha256(path.read_bytes()).hexdigest()
        for path in sorted((out / "reports").glob("*.json"))
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workdir", default=None)
    parser.add_argument("--result", default=str(ROOT / "artifacts" / "acceptance-result.json"))
    args = parser.parse_args()

    workdir = Path(args.workdir or tempfile.mkdtemp(prefix="capacity-acceptance-"))
    workdir.mkdir(parents=True, exist_ok=True)
    sample = workdir / "sample"
    generate_seed_directory(sample)

    clean_a = workdir / "clean-a"
    clean_b = workdir / "clean-b"
    assert run_cli("run", "--input", str(sample), "--out", str(clean_a)).returncode == 0
    assert run_cli("run", "--input", str(sample), "--out", str(clean_b)).returncode == 0
    hashes_a = report_hashes(clean_a)
    hashes_b = report_hashes(clean_b)
    deterministic = hashes_a == hashes_b

    rerun = run_cli("run", "--input", str(sample), "--out", str(clean_a))
    idempotent = rerun.returncode == 0 and report_hashes(clean_a) == hashes_a
    verification = verify_output_directory(clean_a)

    crash_results = []
    stages = [
        ("window_aggregation", None),
        ("fixedpoint_conversion", None),
        ("threshold_merge", None),
        ("patch_commit", "patch-001-gold-cpu-threshold"),
    ]
    crash_reports_identical = True
    for stage, patch in stages:
        out = workdir / f"crash-{stage}"
        crash_args = ["run", "--input", str(sample), "--out", str(out), "--crash-at", stage]
        if patch:
            crash_args += ["--crash-patch", patch]
        crashed = run_cli(*crash_args)
        recovered = run_cli("run", "--input", str(sample), "--out", str(out))
        ok_crash = crashed.returncode == 86
        ok_recover = recovered.returncode == 0
        ok_verify = verify_output_directory(out)["ok"]
        identical = report_hashes(out) == hashes_a
        crash_reports_identical &= identical
        running_units = []
        with sqlite3.connect(out / "capacity_state.sqlite") as conn:
            running_units = conn.execute("SELECT unit_key FROM work_units WHERE state='running'").fetchall()
        crash_results.append(
            {
                "stage": stage,
                "crash_exit_code": crashed.returncode,
                "expected_crash": ok_crash,
                "recovery_exit_code": recovered.returncode,
                "recovered": ok_recover and ok_verify and not running_units,
                "reports_match_clean_run": identical,
                "running_units_after_recovery": running_units,
            }
        )

    concurrent_out = workdir / "concurrent"

    def one_process(index: int) -> dict[str, object]:
        result = run_cli("run", "--input", str(sample), "--out", str(concurrent_out))
        return {"index": index, "returncode": result.returncode, "stderr": result.stderr[-500:]}

    with ThreadPoolExecutor(max_workers=4) as pool:
        concurrent_runs = list(pool.map(one_process, range(4)))
    concurrent_ok = all(item["returncode"] == 0 for item in concurrent_runs)
    concurrent_verify = verify_output_directory(concurrent_out)["ok"]
    with sqlite3.connect(concurrent_out / "capacity_state.sqlite") as conn:
        revision_count = conn.execute("SELECT COUNT(*) FROM revisions").fetchone()[0]
        duplicate_ledger_groups = conn.execute(
            """
            SELECT COUNT(*) FROM (
                SELECT revision_id, seq, COUNT(*) AS n
                  FROM ledger_entries GROUP BY revision_id, seq HAVING n > 1
            )
            """
        ).fetchone()[0]

    baseline = json.loads((clean_a / "reports" / "baseline.json").read_text(encoding="utf-8"))
    values = baseline["evaluation"]["values"]
    max_abs = max(Decimal(item["absolute_deviation"]) for item in values)
    max_rel = max(Decimal(item["relative_deviation"]) for item in values)
    budget_violations = [
        item["obs_key"]
        for item in values
        if Decimal(item["absolute_deviation"])
        > Decimal(item["error_upper_bound_physical"]) + Decimal("1e-48")
    ]
    higher_scale_flips = [
        item for item in baseline["evaluation"]["sensitive_records"] if item["would_flip_at_higher_scale"]
    ]
    fixed_decimal_flips = [
        item
        for item in baseline["evaluation"]["conclusions"]
        if item.get("conclusion_flipped_between_fixed_and_decimal")
    ]

    result = {
        "ok": (
            deterministic
            and idempotent
            and verification["ok"]
            and all(item["recovered"] and item["reports_match_clean_run"] for item in crash_results)
            and concurrent_ok
            and concurrent_verify
            and not budget_violations
            and revision_count == 5
            and duplicate_ledger_groups == 0
        ),
        "workdir": str(workdir),
        "python": sys.version.split()[0],
        "sqlite_module": sqlite3.sqlite_version,
        "report_count": len(hashes_a),
        "deterministic_two_output_directories": deterministic,
        "idempotent_rerun_same_directory": idempotent,
        "verification_ok": verification["ok"],
        "verification_check_count": len(verification["checks"]),
        "crash_recovery": crash_results,
        "crash_reports_match_clean_run": crash_reports_identical,
        "concurrency": {
            "processes": 4,
            "all_exit_zero": concurrent_ok,
            "verify_ok": concurrent_verify,
            "revision_count": revision_count,
            "duplicate_ledger_sequence_groups": duplicate_ledger_groups,
            "runs": concurrent_runs,
        },
        "precision": {
            "comparison_count": len(values),
            "max_absolute_deviation": format(max_abs, "f"),
            "max_relative_deviation": format(max_rel, "f"),
            "budget_violation_count": len(budget_violations),
            "budget_violations": budget_violations,
            "sensitive_count": len(baseline["evaluation"]["sensitive_records"]),
            "higher_scale_flip_count": len(higher_scale_flips),
            "fixed_decimal_conclusion_flip_count": len(fixed_decimal_flips),
            "example_higher_scale_flip": next(
                (
                    {
                        "obs_a": item["obs_a"],
                        "obs_b": item["obs_b"],
                        "metric": item["metric"],
                        "difference": item["difference"],
                        "fixed_order": item["fixed_order"],
                        "higher_scale_order": item["higher_scale_order"],
                        "ledger_seqs": item["ledger_seqs"],
                    }
                    for item in higher_scale_flips
                ),
                None,
            ),
        },
    }
    result_path = Path(args.result)
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k not in {"crash_recovery", "concurrency"}}, ensure_ascii=False, indent=2))
    print(f"acceptance_result={result_path}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
