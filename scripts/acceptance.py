from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from columnar_engine.engine import Engine  # noqa: E402


SAMPLE = ROOT / "samples" / "default"
REPORTS = ROOT / "reports"


def _cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "columnar_engine.cli", *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=120,
    )


def _summary_from(stdout: str) -> dict[str, Any]:
    return json.loads(stdout)["summary"]


def main() -> int:
    run_root = Path(tempfile.mkdtemp(prefix="columnar-acceptance-"))
    REPORTS.mkdir(parents=True, exist_ok=True)

    default_db = run_root / "default" / "engine.db"
    default_work = run_root / "default" / "var"
    engine = Engine(default_db, default_work)
    first = engine.run_sample(SAMPLE)
    second = engine.run_sample(SAMPLE)

    budget = Engine(
        run_root / "budget" / "engine.db", run_root / "budget" / "var"
    ).run_paths(
        SAMPLE / "batches.json",
        SAMPLE / "plan.json",
        ROOT / "samples" / "budget_abandon" / "config.json",
    )

    crash_db = run_root / "crash" / "engine.db"
    crash_work = run_root / "crash" / "var"
    crashed = _cli(
        "run",
        "--sample",
        str(SAMPLE),
        "--db",
        str(crash_db),
        "--work-dir",
        str(crash_work),
        "--crash-after-commits",
        "2",
    )
    resumed = _cli(
        "resume",
        "--sample",
        str(SAMPLE),
        "--db",
        str(crash_db),
        "--work-dir",
        str(crash_work),
    )
    resumed_summary = _summary_from(resumed.stdout)

    failure = Engine(
        run_root / "failure" / "engine.db", run_root / "failure" / "var"
    ).run_sample(SAMPLE, fail_shard_id=1)
    failure_snapshot = Engine(
        run_root / "failure" / "engine.db", run_root / "failure" / "var"
    ).snapshot(failure.run_id)

    concurrency_db = run_root / "concurrency" / "engine.db"
    concurrency_work = run_root / "concurrency" / "var"
    command = [
        sys.executable,
        "-m",
        "columnar_engine.cli",
        "run",
        "--sample",
        str(SAMPLE),
        "--db",
        str(concurrency_db),
        "--work-dir",
        str(concurrency_work),
    ]
    processes = [
        subprocess.Popen(
            command,
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for _ in range(2)
    ]
    concurrent_summaries = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=120)
        if process.returncode != 0:
            raise RuntimeError(stderr)
        concurrent_summaries.append(_summary_from(stdout))
    concurrent_sets = [
        set(summary["scheduler"]["committed_this_call"])
        for summary in concurrent_summaries
    ]

    dual_zero = [
        item
        for item in first.error_spectrum["dual_path_checks"]
        if item["precision_sensitive"] and item["difference"] == 0.0
    ]
    report = {
        "environment": {
            "python": sys.version.split()[0],
            "platform": sys.platform,
            "temporary_run_root": str(run_root),
        },
        "fixed_sample": {
            "run_id": first.run_id,
            "status": first.status,
            "shard_status_counts": first.shard_status_counts,
            "rewrite": {
                "before_columns": first.rewrite["before_columns"],
                "after_columns": first.rewrite["after_columns"],
                "removed_columns": first.rewrite["removed_columns"],
                "pushed_predicates": first.rewrite["pushed_predicates"],
                "equivalence": first.equivalence,
            },
            "scheduler_waves": first.scheduler["waves"],
            "result": first.result["columns"],
            "final_digest": first.final_digest,
        },
        "repeat_run": {
            "status": second.status,
            "committed_this_call": second.scheduler["committed_this_call"],
            "same_final_digest": second.final_digest == first.final_digest,
            "same_result": second.result == first.result,
        },
        "budget": {
            "status": budget.status,
            "shard_status_counts": budget.shard_status_counts,
            "abandoned": budget.scheduler["abandoned"],
        },
        "recovery": {
            "crash_exit_code": crashed.returncode,
            "resume_status": resumed_summary["status"],
            "resume_committed_this_call": resumed_summary["scheduler"][
                "committed_this_call"
            ],
            "same_final_digest": resumed_summary["final_digest"] == first.final_digest,
            "same_result": resumed_summary["result"] == first.result,
        },
        "failure_isolation": {
            "status": failure.status,
            "shard_status_counts": failure.shard_status_counts,
            "node_watermarks": {
                state["node_id"]: state["watermark"]
                for state in failure_snapshot["node_states"]
            },
            "error_codes": [item["code"] for item in failure_snapshot["errors"]],
        },
        "concurrency": {
            "statuses": [item["status"] for item in concurrent_summaries],
            "committed_sets": [sorted(values) for values in concurrent_sets],
            "disjoint": concurrent_sets[0].isdisjoint(concurrent_sets[1]),
            "union": sorted(concurrent_sets[0] | concurrent_sets[1]),
        },
        "diagnostics": {
            "precision_sensitive_count": first.error_spectrum[
                "precision_sensitive_count"
            ],
            "rounding_mode": first.error_spectrum["rounding_mode"],
            "nan_policy": first.error_spectrum["nan_policy"],
            "dual_path_zero_difference_examples": dual_zero[:2],
            "error_sources_fixed_total": first.result["error_sources"]["fixed_total"],
        },
    }
    report_path = REPORTS / "acceptance.json"
    report_path.write_text(
        json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=True),
        encoding="utf-8",
    )

    lines = [
        "Columnar engine acceptance",
        f"Python {report['environment']['python']} on {report['environment']['platform']}",
        f"Sample run: {first.status}, shards={first.shard_status_counts}, digest={first.final_digest}",
        f"Rewrite: removed={first.rewrite['removed_columns']}, pushed={[item['from_node'] for item in first.rewrite['pushed_predicates']]}, equivalent={first.equivalence['equivalent']}",
        f"Repeat run: {second.status}, newly_committed={second.scheduler['committed_this_call']}, same_digest={second.final_digest == first.final_digest}",
        f"Budget: {budget.status}, abandoned={budget.scheduler['abandoned']}",
        f"Recovery: crash_exit={crashed.returncode}, resume={resumed_summary['status']}, same_digest={resumed_summary['final_digest'] == first.final_digest}",
        f"Failure isolation: {failure.shard_status_counts}, watermarks={report['failure_isolation']['node_watermarks']}",
        f"Concurrency: committed_sets={report['concurrency']['committed_sets']}, disjoint={report['concurrency']['disjoint']}",
        f"Diagnostics: precision_sensitive={report['diagnostics']['precision_sensitive_count']}, rounding={report['diagnostics']['rounding_mode']}",
        f"Full report: {report_path}",
    ]
    text = "\n".join(lines) + "\n"
    (REPORTS / "acceptance.txt").write_text(text, encoding="utf-8")
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

