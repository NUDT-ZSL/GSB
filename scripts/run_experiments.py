#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
EXPERIMENT_ROOT = ROOT / "artifacts" / "experiments"
PYTHON = str(ROOT / ".venv" / "bin" / "python")


def run_cli(args: list[str], expect: int = 0) -> dict[str, Any]:
    completed = subprocess.run(
        [PYTHON, "-m", "stream_reopt", *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != expect:
        raise RuntimeError(
            f"command failed: {args}, rc={completed.returncode}, stderr={completed.stderr}, stdout={completed.stdout[:1000]}"
        )
    return json.loads(completed.stdout)


def database(name: str) -> Path:
    return EXPERIMENT_ROOT / f"{name}.db"


def clean_database(name: str) -> Path:
    path = database(name)
    for suffix in ("", "-wal", "-shm", ".lock"):
        candidate = Path(str(path) + suffix)
        if candidate.exists():
            candidate.unlink()
    return path


def batch_manifest_summary() -> dict[str, Any]:
    from stream_reopt.generator import DeterministicGenerator
    from stream_reopt.sample import load_sample

    generator = DeterministicGenerator(load_sample(ROOT / "samples" / "reopt_demo.json"))
    first = generator.manifest()
    second = DeterministicGenerator(load_sample(ROOT / "samples" / "reopt_demo.json")).manifest()
    return {
        "manifest_sha256": first["manifest_sha256"],
        "repeat_identical": first == second,
        "batches": [
            {
                "arrival": item["arrival"],
                "table": item["table"],
                "row_count": item["row_count"],
                "digest": item["digest"],
            }
            for item in first["batches"]
        ],
    }


def main() -> int:
    EXPERIMENT_ROOT.mkdir(parents=True, exist_ok=True)
    environment = subprocess.run(
        [PYTHON, "scripts/verify_environment.py"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    env_payload = json.loads(environment.stdout)
    manifest = batch_manifest_summary()

    main_db = clean_database("main")
    main_run = run_cli(
        [
            "run",
            "--sample",
            "samples/reopt_demo.json",
            "--db",
            str(main_db),
            "--batches",
            "9",
        ]
    )
    main_status = run_cli(
        ["status", "--sample", "samples/reopt_demo.json", "--db", str(main_db)]
    )
    main_verify = run_cli(
        ["verify", "--sample", "samples/reopt_demo.json", "--db", str(main_db)]
    )

    deterministic_db = clean_database("deterministic")
    run_cli(
        [
            "run",
            "--sample",
            "samples/reopt_demo.json",
            "--db",
            str(deterministic_db),
            "--batches",
            "9",
        ]
    )
    deterministic_status = run_cli(
        ["status", "--sample", "samples/reopt_demo.json", "--db", str(deterministic_db)]
    )

    budget_db = clean_database("budget")
    budget_run = run_cli(
        [
            "run",
            "--sample",
            "samples/budget_exhausted.json",
            "--db",
            str(budget_db),
            "--batches",
            "9",
        ],
        expect=3,
    )
    budget_status = run_cli(["status", "--db", str(budget_db)])

    precision_db = clean_database("precision")
    precision_run = run_cli(
        [
            "run",
            "--sample",
            "samples/precision_sensitive.json",
            "--db",
            str(precision_db),
            "--batches",
            "9",
        ]
    )
    precision_status = run_cli(
        ["status", "--sample", "samples/precision_sensitive.json", "--db", str(precision_db)]
    )

    crash_db = clean_database("crash")
    crash_process = subprocess.run(
        [
            PYTHON,
            "-m",
            "stream_reopt",
            "run",
            "--sample",
            "samples/reopt_demo.json",
            "--db",
            str(crash_db),
            "--batches",
            "9",
            "--crash-before-commit-arrival",
            "5",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    crash_status = run_cli(["status", "--db", str(crash_db)])
    crash_resume = run_cli(
        [
            "resume",
            "--sample",
            "samples/reopt_demo.json",
            "--db",
            str(crash_db),
            "--batches",
            "9",
        ]
    )
    crash_verify = run_cli(
        ["verify", "--sample", "samples/reopt_demo.json", "--db", str(crash_db)]
    )
    crash_final_status = run_cli(["status", "--db", str(crash_db)])

    parallel_db = clean_database("parallel")
    parallel_run = run_cli(
        [
            "run",
            "--sample",
            "samples/reopt_demo.json",
            "--db",
            str(parallel_db),
            "--batches",
            "9",
            "--workers",
            "4",
        ]
    )
    parallel_status = run_cli(["status", "--db", str(parallel_db)])
    parallel_verify = run_cli(
        ["verify", "--sample", "samples/reopt_demo.json", "--db", str(parallel_db)]
    )

    from stream_reopt.engine import Engine
    from stream_reopt.sample import load_sample

    idempotent_db = clean_database("idempotent")
    sample = load_sample(ROOT / "samples" / "reopt_demo.json")
    with Engine(sample, idempotent_db) as engine:
        first = engine.commit_arrival(1)
        duplicate = engine.commit_arrival(1)
        idempotent_observations = len(engine.storage.observations())
        idempotent_watermark = engine.storage.state().committed_count

    status_first = subprocess.run(
        [PYTHON, "-m", "stream_reopt", "status", "--db", str(main_db)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    ).stdout
    status_second = subprocess.run(
        [PYTHON, "-m", "stream_reopt", "status", "--db", str(main_db)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    ).stdout

    pytest_process = subprocess.run(
        [PYTHON, "-m", "pytest"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    pytest_last_line = pytest_process.stdout.strip().splitlines()[-1] if pytest_process.stdout.strip() else ""

    switched = [record for record in main_status["reopt_records"] if record["outcome"] == "switched"]
    cooldown = [
        record for record in main_status["drift_records"] if record["decision"] == "cooldown_suppressed"
    ]
    budget_record = next(
        record
        for record in budget_status["reopt_records"]
        if record["outcome"] == "kept_original_budget"
    )
    precision_record = next(
        record for record in precision_status["reopt_records"] if record["precision_sensitive"]
    )
    report = {
        "ok": bool(
            environment.returncode == 0
            and env_payload["ok"]
            and main_verify["ok"]
            and crash_verify["ok"]
            and parallel_verify["ok"]
            and main_status == deterministic_status
            and pytest_process.returncode == 0
        ),
        "environment": env_payload,
        "batch_generation": manifest,
        "main_experiment": {
            "processed": main_run["processed"],
            "watermark": main_status["watermark"],
            "final_shape_version": main_status["shape_version"],
            "current_shape": main_status["current_shape"],
            "switches": [
                {
                    "seq": record["seq"],
                    "from_version": record["from_version"],
                    "to_version": record["to_version"],
                    "from_shape": record["from_shape"],
                    "to_shape": record["to_shape"],
                    "switch_cost": record["switch_cost"],
                    "precision_sensitive": record["precision_sensitive"],
                    "trigger_sources": [
                        {
                            "table": source["source"]["table"],
                            "join_key": source["source"]["join_key"],
                            "drift_ratio": source["drift_ratio"],
                            "batch_interval": source["batch_interval"],
                        }
                        for source in record["trigger_sources"]
                    ],
                }
                for record in switched
            ],
            "cooldown_suppressed": [
                {
                    "seq": record["seq"],
                    "sources": [
                        {
                            "table": source["source"]["table"],
                            "join_key": source["source"]["join_key"],
                            "drift_ratio": source["drift_ratio"],
                        }
                        for source in record["sources"]
                    ],
                    "rejected_candidates": record["rejected"],
                }
                for record in cooldown
            ],
            "observations": [
                {
                    "seq": item["seq"],
                    "input_rows": item["input_rows"],
                    "output_rows": item["output_rows"],
                    "observed_selectivity": item["observed_selectivity"],
                    "error_bound": item["error_chain"]["accumulated_error_bound"],
                    "result_digest": item["result_digest"],
                }
                for item in main_status["observations"]
            ],
            "verify": main_verify,
            "deterministic_status_equal": main_status == deterministic_status,
        },
        "budget_experiment": {
            "return_code": 3,
            "error_code": budget_run["error"]["code"],
            "processed": budget_run["processed"],
            "retained_shape_version": budget_status["shape_version"],
            "decision": budget_record,
        },
        "precision_experiment": {
            "precision_sensitive": precision_status["precision_sensitive"],
            "decision_seq": precision_record["seq"],
            "cost_difference": precision_record["error_chain"]["top_two_cost_difference"],
            "float_tolerance": precision_record["error_chain"]["float_tolerance"],
            "top_two_error_bounds": precision_record["error_chain"]["top_two_error_bounds"],
            "participating_tables": precision_record["error_chain"]["participating_tables"],
            "batch_interval": precision_record["error_chain"]["batch_interval"],
            "steps": precision_record["error_chain"]["steps"],
            "processed": precision_run["processed"],
        },
        "crash_recovery_experiment": {
            "crash_return_code": crash_process.returncode,
            "watermark_before_resume": crash_status["watermark"],
            "shape_version_before_resume": crash_status["shape_version"],
            "resumed_batches": crash_resume["processed"],
            "watermark_after_resume": crash_final_status["watermark"],
            "shape_version_after_resume": crash_final_status["shape_version"],
            "verify": crash_verify,
        },
        "parallel_experiment": {
            "workers": parallel_run["workers"],
            "processed": parallel_run["processed"],
            "unique_batches": len({batch["arrival"] for batch in parallel_status["committed_batches"]}),
            "committed_batches": parallel_status["watermark"]["committed_batches"],
            "unexpected_errors": parallel_run.get("errors", []),
            "verify": parallel_verify,
        },
        "idempotency_experiment": {
            "first_committed": first.committed,
            "first_idempotent": first.idempotent,
            "duplicate_committed": duplicate.committed,
            "duplicate_idempotent": duplicate.idempotent,
            "observation_count": idempotent_observations,
            "watermark": idempotent_watermark,
        },
        "status_stability": {
            "identical": status_first == status_second,
            "sort_keys_enabled": True,
        },
        "pytest": {
            "return_code": pytest_process.returncode,
            "summary": pytest_last_line,
        },
    }
    output = ROOT / "artifacts" / "validation_report.json"
    output.write_text(json.dumps(report, sort_keys=True, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"ok": report["ok"], "report": str(output)}, sort_keys=True, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
