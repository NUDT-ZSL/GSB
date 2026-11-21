"""Two-run deterministic demonstration of the local MV engine."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mvengine.errors import EngineError
from mvengine.package import load_package
from mvengine.samplegen import generate_suite
from mvengine.storage import (
    initialize,
    query,
    refresh_logs,
    replace_package,
    resume,
    status,
    verify,
)
from mvengine.util import canonical_json, digest_json, pretty_json


SAMPLES = ROOT / "samples"
WORK = ROOT / "artifacts" / "demo-work"


def _view_metrics(result: dict[str, Any]) -> dict[str, Any]:
    return {
        item["name"]: {
            "metrics": item["metrics"],
            "result_digest": item["result_digest"],
            "visible": item["visible"],
        }
        for item in result["views"]
    }


def _run_once(run_name: str) -> dict[str, Any]:
    work = WORK / run_name
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)

    base = load_package(SAMPLES / "sample" / "package.json")
    append = load_package(SAMPLES / "sample_append" / "package.json")
    next_package = load_package(SAMPLES / "sample_next" / "package.json")
    rollback = load_package(SAMPLES / "sample_rollback" / "package.json")

    main_db = work / "main.sqlite"
    initialize(base, main_db)
    refresh_result = refresh_logs(db_path=main_db, logs=base.logs)
    main_status = status(main_db)
    main_query = query(db_path=main_db)
    main_verify = verify(
        db_path=main_db,
        logs=base.logs,
        snapshots_dir=base.snapshots_path,
    )

    rollback_code = None
    try:
        replace_package(db_path=main_db, package=rollback)
    except EngineError as exc:
        rollback_code = exc.code

    replacement = replace_package(db_path=main_db, package=next_package)
    next_status = status(main_db)
    next_query = query(db_path=main_db)

    append_db = work / "append.sqlite"
    initialize(base, append_db)
    refresh_logs(db_path=append_db, logs=base.logs)
    append_classification = replace_package(db_path=append_db, package=append)["classification"]
    append_refresh = refresh_logs(db_path=append_db, logs=append.logs)
    append_query = query(db_path=append_db)

    interrupted_db = work / "interrupted.sqlite"
    initialize(base, interrupted_db)
    command = [
        sys.executable,
        "-m",
        "mvengine",
        "refresh",
        "--db",
        str(interrupted_db),
        "--package",
        str(SAMPLES / "sample" / "package.json"),
        "--kill-after",
        "4",
    ]
    killed = subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    interrupted_status = status(interrupted_db)
    resumed = resume(db_path=interrupted_db, package=base, logs=base.logs)
    resumed_status = status(interrupted_db)
    resumed_verify = verify(db_path=interrupted_db, logs=base.logs)

    return {
        "append": {
            "classification": append_classification,
            "metrics": {
                item["name"]: item["metrics"]
                for item in append_query["views"]
            },
            "refresh_applied": append_refresh["applied"],
        },
        "initial_refresh": {
            "applied_count": len(refresh_result["applied"]),
            "watermark_seq": refresh_result["watermark_seq"],
        },
        "interruption": {
            "exit_code": killed.returncode,
            "incomplete_before_resume": interrupted_status["incomplete_refresh_runs"],
            "recovered_runs": resumed["recovered_runs"],
            "resume_applied": resumed["applied"],
            "watermark_after_resume": resumed_status["watermark_seq"],
            "watermark_before_resume": interrupted_status["watermark_seq"],
            "verify": resumed_verify["ok"],
        },
        "main_status": {
            "current_package_version": main_status["current_package_version"],
            "index_digest": main_status["index_digest"],
            "migration_decision_count": main_status["migration_decision_count"],
            "view_generation_floor": main_status["view_generation_floor"],
            "views": [
                {
                    "applied_seq": item["applied_seq"],
                    "generation": item["generation"],
                    "name": item["name"],
                    "recompute_count": item["recompute_count"],
                    "status": item["status"],
                }
                for item in main_status["views"]
            ],
            "watermark_seq": main_status["watermark_seq"],
        },
        "main_verify": main_verify["ok"],
        "main_view_metrics": _view_metrics(main_query),
        "replacement": {
            "actions": replacement["replacement_actions"],
            "classification": replacement["classification"],
            "view_generations": {
                item["name"]: item["generation"] for item in next_status["views"]
            },
        },
        "replacement_metrics": _view_metrics(next_query),
        "rollback_error": rollback_code,
        "snapshot_checks": [
            {
                "name": item["name"],
                "ok": item["ok"],
                "watermark_seq": item["watermark_seq"],
            }
            for item in main_verify["snapshot_results"]
        ],
    }


def main() -> int:
    generate_suite(SAMPLES)
    first = _run_once("run-1")
    second = _run_once("run-2")
    first_bytes = (canonical_json(first) + "\n").encode("utf-8")
    second_bytes = (canonical_json(second) + "\n").encode("utf-8")
    if first_bytes != second_bytes:
        sys.stderr.write("demo transcripts differ\n")
        return 1
    artifacts = ROOT / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    (artifacts / "demo-run-1.json").write_bytes(first_bytes)
    (artifacts / "demo-run-2.json").write_bytes(second_bytes)
    report = {
        "reproducible": True,
        "runs": 2,
        "summary": first,
        "transcript_sha256": digest_json(first),
    }
    (artifacts / "demo-verification.json").write_text(
        pretty_json(report) + "\n",
        encoding="utf-8",
    )
    sys.stdout.write(pretty_json(report) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

